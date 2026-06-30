import asyncio
import sys
import signal
import httpx
import config
from data_layer import fetch_viable_pairs, fetch_ticker, fetch_ohlcv, fetch_all_tickers
from indicators import compute_signals, compute_batch_signals
from llm_filter import evaluate_portfolio
from risk_manager import RiskManager, PortfolioRiskManager
from executor import place_order, get_balance
from deadman import refresh_deadman, cancel_deadman
from notifier import send_message
from db import init_db, log_trade, log_decision

risk = RiskManager()
portfolio_risk = PortfolioRiskManager()
positions: list[dict] = []
shutdown_flag = False

def handle_sig(*_):
    global shutdown_flag
    shutdown_flag = True

def pnl_pct(entry: float, current: float, side: str) -> float:
    if side.upper() == "BUY":
        return (current - entry) / entry * 100
    return (entry - current) / entry * 100

async def portfolio_cycle(client: httpx.AsyncClient):
    global positions

    try:
        print("Scanning market for viable pairs...", flush=True)
        viable = await fetch_viable_pairs(client)
        print(f"Found {len(viable)} viable IDR pairs", flush=True)

        if not viable:
            print("No viable pairs. Skipping cycle.", flush=True)
            return

        print("Fetching OHLCV with concurrency limit...", flush=True)
        sem = asyncio.Semaphore(config.OHLCV_FETCH_CONCURRENCY)
        ohlcv_map: dict[str, list[dict]] = {}
        ticker_map: dict[str, dict] = {}

        async def fetch_one(v: dict):
            pid = v["pair"]
            async with sem:
                try:
                    ohlcv = await fetch_ohlcv(client, pair=pid, tf=60, limit=100)
                    if ohlcv and len(ohlcv) >= 30:
                        ohlcv_map[pid] = ohlcv
                        ticker_map[pid] = v["ticker"]
                except Exception as e:
                    print(f"  {pid}: {e}", flush=True)

        await asyncio.gather(*[fetch_one(v) for v in viable])

        print(f"Computing signals for {len(ohlcv_map)} pairs...", flush=True)
        all_signals = compute_batch_signals(ohlcv_map)

        actual_idr_balance = 100_000
        if not config.PAPER_TRADING and config.INDODAX_API_KEY:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                actual_idr_balance = float(bal.get("idr", 0))
            except Exception as e:
                print(f"Balance fetch error: {e}", flush=True)

        balance_idr = min(actual_idr_balance, config.PLAY_CAPITAL_IDR)
        if balance_idr < config.MIN_ORDER_IDR and not any(p.get("active") for p in positions):
            balance_idr = config.PLAY_CAPITAL_IDR

        total_equity = balance_idr + sum(
            p["qty"] * ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            for p in positions
        )

        for p in positions:
            last = ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            p["pnl_pct"] = round(pnl_pct(p["entry_price"], last, p["side"]), 2)

        if portfolio_risk.check_portfolio_stop(total_equity):
            msg = (f"PORTFOLIO STOP-LOSS HIT ({config.PORTFOLIO_STOP_LOSS_PCT*100}%)\n"
                   f"Equity: Rp{total_equity:,.0f}\nClosing all positions.")
            await send_message(msg)
            positions.clear()
            print("Portfolio stop-loss triggered. Positions cleared.", flush=True)

        if risk.should_stop_trading(balance_idr):
            await send_message(f"Daily loss limit reached. Bot stopped.")
            sys.exit(0)

        current_positions_info = [
            {"pair": p["pair"], "side": p["side"], "entry_price": p["entry_price"],
             "qty": p["qty"], "pnl_pct": p.get("pnl_pct", 0)}
            for p in positions
        ]

        print("Calling DeepSeek portfolio manager...", flush=True)
        portfolio_pnl = ((total_equity - config.INITIAL_CAPITAL_IDR) / config.INITIAL_CAPITAL_IDR * 100
                         if config.INITIAL_CAPITAL_IDR else 0)
        decision = evaluate_portfolio(all_signals, ticker_map, current_positions_info,
                                       balance_idr, portfolio_pnl)
        print(f"PM decision: {decision.get('decision')} | {decision.get('reasoning', '')[:100]}", flush=True)

        log_decision("PORTFOLIO", decision.get("decision", "HOLD"),
                     decision.get("reasoning", ""),
                     executed=len(decision.get("trades", [])) > 0)

        sl_hits = []
        for p in list(positions):
            last = ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            result = risk.check_sl_tp(p["entry_price"], last, p["side"])
            if result:
                pnl = (last - p["entry_price"]) * p["qty"]
                if p["side"] == "SELL":
                    pnl = (p["entry_price"] - last) * p["qty"]
                sl_hits.append(f"{p['pair']} {result}: {pnl:+.0f} IDR")
                positions.remove(p)
                log_trade(p["side"], last, p["qty"], p["amount_idr"],
                          status="closed", pnl=pnl, reason=result)

        if sl_hits:
            await send_message("SL/TP triggered:\n" + "\n".join(sl_hits))

        trades = decision.get("trades", [])
        if not trades:
            print("No trades suggested. Sleeping.", flush=True)
            if positions and config.INDODAX_API_KEY:
                await refresh_deadman(client)
            return

        valid_trades = portfolio_risk.validate_allocation(trades, current_positions_info, balance_idr)
        if not valid_trades:
            print("No valid trades after risk checks.", flush=True)
            return

        executed_trades = []
        for t in valid_trades:
            pid = t["pair"]
            action = t["action"]
            alloc = t["allocation_pct"]
            amount = balance_idr * (alloc / 100)
            ticker = ticker_map.get(pid, {})
            price = ticker.get("sell" if action == "BUY" else "buy", 0)
            if not price:
                continue
            qty = amount / price

            if not risk.is_profit_viable(price, qty, action):
                print(f"  {pid}: skipped - fees eat profit", flush=True)
                continue

            print(f"  {action} {pid} @ {price} | Rp{amount:,.0f} ({qty:.6f}) | alloc: {alloc}%", flush=True)

            order = await place_order(client, action.lower(), price, amount,
                                       pair=pid, order_type="market" if config.PAPER_TRADING else "limit")
            log_trade(action.lower(), price, qty, amount,
                      order_type="market" if config.PAPER_TRADING else "limit",
                      status="simulated" if config.PAPER_TRADING else "placed",
                      reason=t.get("reason", ""))
            executed_trades.append(t)

            if action == "BUY":
                positions.append({
                    "pair": pid,
                    "side": action,
                    "entry_price": price,
                    "qty": qty,
                    "amount_idr": amount,
                })
            elif action == "SELL":
                positions = [p for p in positions if p["pair"] != pid]

        if executed_trades:
            msg_lines = [f"{'[PAPER] ' if config.PAPER_TRADING else ''}PORTFOLIO REBALANCE"]
            for t in executed_trades:
                msg_lines.append(f"{t['action']} {t['pair']} ({t['allocation_pct']}%) — {t['reason'][:60]}")
            msg_lines.append(f"Positions: {len(positions)} | Cash: Rp{balance_idr:,.0f}")
            await send_message("\n".join(msg_lines))
            print(f"Portfolio: {len(positions)} positions | Cash: Rp{balance_idr:,.0f}", flush=True)

        if positions and config.INDODAX_API_KEY:
            await refresh_deadman(client)

    except Exception as e:
        print(f"Portfolio cycle error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await send_message(f"Portfolio cycle error: {e}")
        except Exception:
            pass

async def main():
    global shutdown_flag

    print("=" * 50, flush=True)
    print("  AI HEDGE FUND MANAGER — INDODAX", flush=True)
    print(f"  Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}", flush=True)
    print(f"  Play capital: Rp{config.PLAY_CAPITAL_IDR:,} (FIXED)", flush=True)
    print(f"  Model: {config.DEEPSEEK_MODEL}", flush=True)
    print(f"  Max positions: {config.MAX_OPEN_POSITIONS}", flush=True)
    print(f"  Fundamental coins: {len(config.FUNDAMENTAL_COINS)}", flush=True)
    print("=" * 50, flush=True)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        init_db()
        print("DB init OK", flush=True)
    except Exception as e:
        print(f"DB init failed: {e}", flush=True)

    ok = await send_message(
        f"Hedge Fund Manager started\n"
        f"Play capital: Rp{config.PLAY_CAPITAL_IDR:,} (fixed)\n"
        f"Fundamental coins only ({len(config.FUNDAMENTAL_COINS)} pairs)\n"
        f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}"
    )
    print(f"Telegram: {'OK' if ok else 'FAILED'}", flush=True)

    async def telegram_poller():
        last_id = 0
        while not shutdown_flag:
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates",
                        json={"offset": last_id + 1, "timeout": 30},
                    )
                    if r.status_code == 200:
                        for upd in r.json().get("result", []):
                            last_id = upd["update_id"]
                            txt = (upd.get("message", {}).get("text") or "").strip().lower()
                            cid = upd.get("message", {}).get("chat", {}).get("id")
                            if txt in ("/start", "/status"):
                                pos_text = "No positions"
                                if positions:
                                    pos_text = "\n".join(
                                        f"{p['pair']} {p['side']} @ {p['entry_price']} ({p.get('pnl_pct', 0):+.2f}%)"
                                        for p in positions[:10]
                                    )
                                text = (f"AI Hedge Fund Manager\n"
                                        f"Status: {'PAPER' if config.PAPER_TRADING else 'LIVE'}\n"
                                        f"Play capital: Rp{config.PLAY_CAPITAL_IDR:,} (fixed)\n"
                                        f"Positions: {len(positions)}\n{pos_text}")
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": text},
                                    )
            except Exception:
                pass
            await asyncio.sleep(5)

    async with httpx.AsyncClient(timeout=30) as client:
        poller = asyncio.create_task(telegram_poller())
        cycle_count = 0

        while not shutdown_flag:
            cycle_count += 1
            print(f"\n{'='*20} Cycle #{cycle_count} {'='*20}", flush=True)
            await portfolio_cycle(client)
            for _ in range(config.LOOP_INTERVAL_SECONDS // 5):
                if shutdown_flag:
                    break
                await asyncio.sleep(5)

    poller.cancel()
    if config.INDODAX_API_KEY:
        await cancel_deadman(client)
    print("Shutdown complete.", flush=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    except Exception as e:
        print(f"Fatal: {e}", flush=True)
        import traceback
        traceback.print_exc()
