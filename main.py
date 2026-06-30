import asyncio
import hashlib
import hmac
import sys
import signal
import time
from urllib.parse import urlencode
import httpx
import config
from data_layer import fetch_viable_pairs, fetch_ticker, fetch_ohlcv, fetch_ohlcv_both, fetch_all_tickers, fetch_orderbook
from indicators import compute_signals, compute_batch_signals
from llm_filter import evaluate_portfolio
from pairs import compute_all_pairs
from risk_manager import RiskManager, PortfolioRiskManager
from executor import place_order, get_balance, get_order
from deadman import refresh_deadman, cancel_deadman
from notifier import send_message
from db import init_db, log_trade, log_decision
from market_ws import market_ws_loop, LIVE_TICKERS, stop as mws_stop
from private_ws import private_ws_loop, stop as pws_stop

risk = RiskManager()
portfolio_risk = PortfolioRiskManager()
positions: list[dict] = []
external_positions: list[dict] = []
shutdown_flag = False

regime_history: list[str] = []
known_pairs: set[str] = set()

def classify_regime(all_signals: dict) -> dict:
    signals = [s.get("raw_signal") for s in all_signals.values() if s.get("raw_signal")]
    scores = [s.get("score", 0) for s in all_signals.values() if s.get("score") is not None]
    vols = [s.get("volatility", 0) for s in all_signals.values() if s.get("volatility") is not None]

    buys = signals.count("BUY")
    sells = signals.count("SELL")
    total = len(signals) or 1
    avg_score = sum(scores) / len(scores) if scores else 0
    avg_vol = sum(vols) / len(vols) if vols else 0
    high_conviction = sum(1 for s in all_signals.values() if s.get("conviction") == "HIGH")

    if buys / total >= 0.4 and avg_score > 0:
        regime = "BULL"
    elif buys / total >= 0.25 and avg_score > 0 and high_conviction >= 2:
        regime = "BULL"
    elif sells / total >= 0.5 and avg_score < -1:
        regime = "BEAR"
    elif avg_vol > config.HIGH_VOL_THRESHOLD:
        regime = "HIGH_VOL"
    elif avg_vol < 0.5 and buys / total < 0.2 and sells / total < 0.2:
        regime = "SIDEWAYS_LOW_VOL"
    else:
        regime = "SIDEWAYS"

    return {
        "regime": regime,
        "buy_ratio": round(buys / total, 2),
        "sell_ratio": round(sells / total, 2),
        "avg_score": round(avg_score, 1),
        "avg_volatility": round(avg_vol, 2),
        "high_conviction_count": high_conviction,
        "total_signals": total,
    }

def compute_pairs_suggestions(all_signals: dict, ticker_map: dict) -> list[dict]:
    suggestions = []
    for a, b in config.CORRELATION_PAIRS:
        ta = ticker_map.get(a, {}).get("last", 0)
        tb = ticker_map.get(b, {}).get("last", 0)
        if not ta or not tb:
            continue
        ratio = ta / tb
        suggestions.append({
            "pair": f"{a}/{b}", "ratio": round(ratio, 4),
            "a_price": ta, "b_price": tb,
        })
    return suggestions

def handle_sig(*_):
    global shutdown_flag
    shutdown_flag = True

def pnl_pct(entry: float, current: float, side: str) -> float:
    if entry <= 0:
        return 0
    if side.upper() == "BUY":
        return (current - entry) / entry * 100
    return (entry - current) / entry * 100

async def portfolio_cycle(client: httpx.AsyncClient):
    global positions

    try:
        print("Scanning market for viable pairs...", flush=True)
        viable = await fetch_viable_pairs(client)
        print(f"Found {len(viable)} viable IDR pairs", flush=True)
        current_pairs = {v["pair"] for v in viable}
        new_coins = current_pairs - known_pairs
        if new_coins:
            print(f"New coins detected: {', '.join(new_coins)}", flush=True)
        known_pairs.update(current_pairs)

        live = LIVE_TICKERS.copy()
        if live:
            for v in viable:
                pid = v["pair"]
                if pid in live and v["ticker"].get("last", 0) == 0:
                    v["ticker"] = live[pid]

        if not viable:
            print("No viable pairs. Skipping cycle.", flush=True)
            return

        print("Fetching OHLCV (1h + 4h) with concurrency limit...", flush=True)
        sem = asyncio.Semaphore(config.OHLCV_FETCH_CONCURRENCY)
        ohlcv_map_1h: dict[str, list[dict]] = {}
        ohlcv_map_4h: dict[str, list[dict]] = {}
        ticker_map: dict[str, dict] = {}

        async def fetch_one(v: dict):
            pid = v["pair"]
            async with sem:
                try:
                    o1, o4 = await fetch_ohlcv_both(client, pair=pid)
                    if len(o1) >= 30:
                        ohlcv_map_1h[pid] = o1
                        ticker_map[pid] = v["ticker"]
                    if len(o4) >= 30:
                        ohlcv_map_4h[pid] = o4
                except Exception as e:
                    print(f"  {pid}: {e}", flush=True)

        await asyncio.gather(*[fetch_one(v) for v in viable])

        print(f"Computing signals: {len(ohlcv_map_1h)} pairs (1h) + {len(ohlcv_map_4h)} (4h)...", flush=True)
        all_signals = compute_batch_signals(ohlcv_map_1h, ohlcv_map_4h)

        pair_signals = compute_all_pairs(ohlcv_map_1h)
        active = [p for p in pair_signals if p["signal"] in ("SHORT_SPREAD", "LONG_SPREAD")]
        if active:
            for ps in active:
                print(f"PAIR SIGNAL: {ps['pair']} → {ps['signal']} (z={ps['z_score']})", flush=True)
        elif pair_signals:
            for ps in pair_signals[:3]:
                print(f"Pair: {ps['pair']} z={ps['z_score']} ({ps['signal']})", flush=True)

        regime_info = classify_regime(all_signals)
        regime_history.append(regime_info["regime"])
        if len(regime_history) > config.REGIME_LOOKBACK_CYCLES:
            regime_history.pop(0)
        print(f"Regime: {regime_info['regime']} | B:{regime_info['buy_ratio']} S:{regime_info['sell_ratio']} "
              f"Score:{regime_info['avg_score']} HC:{regime_info['high_conviction_count']}", flush=True)

        pair_suggestions = compute_pairs_suggestions(all_signals, ticker_map)
        if pair_suggestions:
            pair_str = " ".join(f"{p['pair']}={p['ratio']}" for p in pair_suggestions)
            print(f"Pairs: {pair_str}", flush=True)

        external_positions.clear()
        actual_idr_balance = 100_000
        if config.INDODAX_API_KEY and config.INDODAX_SECRET_KEY:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                actual_idr_balance = float(bal.get("idr", 0))
                for coin, raw_qty in bal.items():
                    qty = float(raw_qty)
                    if qty <= 0 or coin == "idr":
                        continue
                    pair = f"{coin}_idr"
                    if pair in config.STABLECOINS:
                        continue
                    if any(p["pair"] == pair for p in positions):
                        continue
                    last_price = ticker_map.get(pair, {}).get("last", 0)
                    external_positions.append({
                        "pair": pair, "side": "BUY",
                        "entry_price": 0,
                        "qty": qty,
                        "amount_idr": qty * (last_price or 1),
                        "current_price": last_price,
                        "real": True,
                    })
                if external_positions:
                    ext_summary = ", ".join(f"{p['pair']}({p['qty']})" for p in external_positions)
                    print(f"External positions: {ext_summary}", flush=True)
            except Exception as e:
                print(f"Balance fetch error: {e}", flush=True)

        pending_play_capital_pct = config.DEFAULT_PLAY_CAPITAL_PCT
        balance_idr = int(actual_idr_balance * pending_play_capital_pct)
        paper_equity = sum(
            p["qty"] * ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            for p in positions
        )
        total_equity = actual_idr_balance + paper_equity

        if portfolio_risk.check_portfolio_stop(total_equity):
            msg = (f"PORTFOLIO STOP-LOSS HIT ({config.PORTFOLIO_STOP_LOSS_PCT*100}%)\n"
                   f"Equity: Rp{total_equity:,.0f}\nClosing all positions.")
            await send_message(msg)
            positions.clear()
            print("Portfolio stop-loss triggered.", flush=True)

        if risk.should_stop_trading(total_equity):
            await send_message(f"Daily loss limit reached. Bot stopped.")
            sys.exit(0)

        all_positions = positions + external_positions
        for p in all_positions:
            last = ticker_map.get(p["pair"], {}).get("last", p.get("current_price") or p.get("entry_price") or 0)
            p["pnl_pct"] = round(pnl_pct(p.get("entry_price") or 0, last, p["side"]), 2) if last else 0

        current_positions_info = [
            {
                "pair": p["pair"],
                "side": p["side"],
                "entry_price": p.get("entry_price") or 0,
                "qty": p["qty"],
                "pnl_pct": p.get("pnl_pct", 0),
                "current_value": p["qty"] * ticker_map.get(p["pair"], {}).get("last", 0),
            }
            for p in all_positions
        ]

        orderbooks = {}
        top_pairs = list(ohlcv_map_1h.keys())[:5]
        for pid in top_pairs:
            try:
                ob = await fetch_orderbook(client, pair=pid, depth=10)
                if ob:
                    orderbooks[pid] = ob
            except Exception:
                pass

        has_active_signal = any(
            s.get("raw_signal") in ("BUY", "SELL") and s.get("score", 0) >= 3 for s in all_signals.values()
        )
        has_external = len(external_positions) > 0

        if not has_active_signal and not has_external:
            decision = {"decision": "HOLD", "reasoning": "All signals HOLD, no external positions — skipping LLM to save cost", "trades": []}
            print("LLM SKIPPED — all HOLD, no external positions", flush=True)
        else:
            print("Calling DeepSeek portfolio manager...", flush=True)
            portfolio_pnl = ((total_equity - config.PLAY_CAPITAL_IDR) / config.PLAY_CAPITAL_IDR * 100
                             if config.PLAY_CAPITAL_IDR else 0)
            decision = evaluate_portfolio(all_signals, ticker_map, current_positions_info,
                                           balance_idr, portfolio_pnl,
                                           regime_info, pair_suggestions, regime_history, orderbooks,
                                           LIVE_TICKERS, new_coins, pair_signals)
            if decision.get("deepseek_error"):
                await send_message(f"⚠️ DeepSeek API error: {decision.get('reasoning', '')[:200]}")
            print(f"PM decision: {decision.get('decision')} | {decision.get('reasoning', '')[:100]}", flush=True)

        play_capital_pct = decision.get("play_capital_pct", pending_play_capital_pct * 100)
        balance_idr = int(actual_idr_balance * play_capital_pct / 100)
        print(f"CIO play capital: {play_capital_pct}% of Rp{actual_idr_balance:,.0f} = Rp{balance_idr:,}", flush=True)

        log_decision("PORTFOLIO", decision.get("decision", "HOLD"),
                     decision.get("reasoning", ""),
                     executed=len(decision.get("trades", [])) > 0)

        sl_hits = []
        for p in list(positions):
            last = ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            atr = p.get("atr_pct")
            result = risk.check_sl_tp(p["entry_price"], last, p["side"])
            if not result and atr:
                dyn_sl = p["entry_price"] * (1 - atr / 100) if p["side"] == "BUY" else p["entry_price"] * (1 + atr / 100)
                if (p["side"] == "BUY" and last <= dyn_sl) or (p["side"] == "SELL" and last >= dyn_sl):
                    result = "ATR_SL"
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
        buy_count = sum(1 for t in trades if t.get("action") == "BUY")
        slots_left = max(0, config.MAX_OPEN_POSITIONS - len(positions))
        if buy_count > slots_left:
            trades = [t for t in trades if t.get("action") == "SELL"] + \
                     [t for t in trades if t.get("action") == "BUY"][:slots_left]
            print(f"Limited buys to {slots_left} (max {config.MAX_OPEN_POSITIONS} positions)", flush=True)

        if not trades:
            brief = (f"CIO: {decision.get('decision')} | Regime: {regime_info.get('regime', 'N/A')}\n"
                     f"Play: {play_capital_pct}% | Pairs: {len(all_signals)}\n"
                     f"{(decision.get('reasoning') or '')[:200]}")
            await send_message(brief)
            print("No trades suggested. Sleeping.", flush=True)
            if positions and config.INDODAX_API_KEY:
                pair_str = ",".join(p["pair"] for p in positions[:5])
                await refresh_deadman(client, pair_str)
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

            ext = next((e for e in external_positions if e["pair"] == pid), None)
            if ext and action == "SELL":
                qty = ext["qty"]
                ticker = ticker_map.get(pid, {})
                price = ticker.get("buy", 0)
                if not price:
                    continue
                print(f"  SELL external {pid} @ {price} | {qty} coin", flush=True)
                try:
                    nonce_sell = int(time.time() * 1000)
                    coin_name = pid.split("_")[0]
                    sell_params = {
                        "method": "trade", "nonce": nonce_sell,
                        "pair": pid, "type": "sell",
                        coin_name: f"{qty:.8f}", "order_type": "market",
                    }
                    sell_body = urlencode(sell_params)
                    sell_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(),
                                         sell_body.encode(), hashlib.sha512).hexdigest()
                    sr = await client.post(config.INDODAX_TAPI_URL, headers={
                        "Key": config.INDODAX_API_KEY, "Sign": sell_sig,
                        "Content-Type": "application/x-www-form-urlencoded",
                    }, content=sell_body)
                    sell_result = sr.json()
                    print(f"  Real sell result: {sell_result}", flush=True)
                    if sell_result.get("success") == 1:
                        received = sell_result["return"].get("receive_rp", 0)
                        external_positions.remove(ext)
                        log_trade("sell", price, qty, float(received), status="closed",
                                  pnl=float(received) - ext.get("amount_idr", 0),
                                  reason=f"CIO decision: {t.get('reason', '')}")
                        executed_trades.append(t)
                        await send_message(f"CIO EKSEKUSI: JUAL {pid}\n"
                                           f"Qty: {qty} | Diterima: Rp{received:,}")
                except Exception as e:
                    print(f"  Failed sell {pid}: {e}", flush=True)
                continue

            match = next((e for e in positions if e["pair"] == pid), None)
            if action == "SELL" and match:
                qty = match["qty"]
                ticker = ticker_map.get(pid, {})
                price = ticker.get("buy", 0)
                if not price:
                    continue
                amount = qty * price
            else:
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

            ohlcv = ohlcv_map_1h.get(pid)
            if ohlcv and action == "BUY":
                atr_pct = risk.compute_atr(ohlcv)
                sl, tp = risk.get_sl_tp(price, action, atr_pct)
                print(f"  ATR: {atr_pct}% | SL: {sl} | TP: {tp}", flush=True)

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
                    "atr_pct": atr_pct if ohlcv else None,
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
            pair_str = ",".join(p["pair"] for p in positions[:5])
            await refresh_deadman(client, pair_str)

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
    print(f"  CIO manages play capital dynamically", flush=True)
    print(f"  Model: {config.DEEPSEEK_MODEL}", flush=True)
    print(f"  Max positions: {config.MAX_OPEN_POSITIONS}", flush=True)
    print(f"  CIO selects coins from top {config.MAX_SCAN_PAIRS} by volume", flush=True)
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
        f"CIO manages play capital dynamically\n"
        f"CIO scans top {config.MAX_SCAN_PAIRS} pairs by volume\n"
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
                                ext_text = ""
                                if positions:
                                    pos_text = "\n".join(
                                        f"{p['pair']} {p['side']} @ {p['entry_price']} ({p.get('pnl_pct', 0):+.2f}%)"
                                        for p in positions[:10]
                                    )
                                if external_positions:
                                    ext_text = "\nExt: " + ", ".join(f"{p['pair']}({p['qty']})" for p in external_positions[:5])
                                text = (f"AI Hedge Fund Manager\n"
                                        f"Status: {'PAPER' if config.PAPER_TRADING else 'LIVE'}\n"
                                        f"CIO manages play capital\n"
                                        f"Paper positions: {len(positions)}\n{pos_text}{ext_text}")
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": text},
                                    )
            except Exception:
                pass
            await asyncio.sleep(5)

    ws_task = asyncio.create_task(market_ws_loop())
    pws_task = asyncio.create_task(private_ws_loop())
    for _ in range(6):
        await asyncio.sleep(0.5)


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
    mws_stop()
    pws_stop()
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
