import asyncio
import sys
import signal
import httpx
import config
from data_layer import fetch_ticker, fetch_orderbook, fetch_ohlcv
from indicators import compute_signals
from llm_filter import evaluate
from risk_manager import RiskManager
from executor import place_order, get_balance, get_open_orders
from deadman import refresh_deadman, cancel_deadman
from notifier import send_message
from db import init_db, log_trade, log_decision, get_recent_trades

risk = RiskManager()
paper_position = {"active": False, "side": "", "entry_price": 0, "qty": 0, "amount_idr": 0}
shutdown_flag = False

def handle_sig(*_):
    global shutdown_flag
    shutdown_flag = True

async def cycle(client: httpx.AsyncClient):
    global paper_position

    try:
        ticker = await fetch_ticker(client)
        if not ticker:
            return

        ohlcv = await fetch_ohlcv(client)

        signals = compute_signals(ohlcv)
        raw_signal = signals["raw_signal"]

        balance_idr = 100_000
        balance_coin = 0.0
        has_position = paper_position["active"]
        entry_price = paper_position["entry_price"] if has_position else None

        if not config.PAPER_TRADING and config.INDODAX_API_KEY:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                base, coin = config.PAIR.split("_")
                balance_idr = float(bal.get(base, 0))
                balance_coin = float(bal.get(coin, 0))
                orders = await get_open_orders(client)
                has_position = len(orders) > 0 or balance_coin > 0.001
            except Exception as e:
                await send_message(f"Balance fetch error: {e}")

        if risk.should_stop_trading(balance_idr):
            await send_message("Daily loss limit reached — bot stopped.")
            sys.exit(0)

        if has_position:
            last = ticker["last"]
            pnl_pct = (last - entry_price) / entry_price * 100 if entry_price else 0
            if paper_position["side"] == "SELL":
                pnl_pct = (entry_price - last) / entry_price * 100 if entry_price else 0
            print(f"Position: {paper_position['side']} | Entry: {entry_price} | Now: {last} | PnL: {pnl_pct:+.2f}%", flush=True)
            sl_check = risk.check_sl_tp(entry_price, last, paper_position["side"])
            if sl_check:
                pnl = (last - entry_price) * paper_position["qty"]
                if paper_position["side"] == "SELL":
                    pnl = (entry_price - last) * paper_position["qty"]
                msg = (f"{'[PAPER] ' if config.PAPER_TRADING else ''}{sl_check}\n"
                       f"Side: {paper_position['side']}\n"
                       f"Entry: {entry_price}\nExit: {last}\n"
                       f"PnL: {pnl:.0f} IDR")
                await send_message(msg)
                log_trade(paper_position["side"], last, paper_position["qty"],
                          paper_position["amount_idr"], status="closed",
                          pnl=pnl, reason=sl_check)
                paper_position["active"] = False
            if config.INDODAX_API_KEY:
                await refresh_deadman(client)

        trade_history = get_recent_trades()

        decision = {"decision": "REJECT", "adjusted_size_pct": None, "reasoning": "No signal"}
        if raw_signal != "HOLD" and not has_position:
            print("Calling DeepSeek LLM filter...", flush=True)
            decision = evaluate(signals, ticker, balance_idr, balance_coin,
                                has_position, entry_price, trade_history)
            print(f"LLM decision: {decision['decision']} | {decision['reasoning'][:80]}", flush=True)
            log_decision(raw_signal, decision["decision"], decision["reasoning"],
                         executed=(decision["decision"] == "CONFIRM"))

            if decision["decision"] == "CONFIRM":
                size_pct = decision.get("adjusted_size_pct")
                amount = balance_idr * (size_pct / 100) if size_pct else risk.compute_position_size(balance_idr)
                amount = min(amount, balance_idr * 0.9)
                price = ticker["sell"] if raw_signal == "BUY" else ticker["buy"]
                qty = amount / price

                if not risk.is_profit_viable(price, qty, raw_signal):
                    decision["decision"] = "REJECT"
                    decision["reasoning"] += "; profit not viable after fees"
                else:
                    order = await place_order(client, raw_signal.lower(), price, amount,
                                              order_type="market" if config.PAPER_TRADING else "limit")
                    log_trade(raw_signal.lower(), price, qty, amount,
                              order_type="market" if config.PAPER_TRADING else "limit",
                              status="simulated" if config.PAPER_TRADING else "placed",
                              reason=decision["reasoning"])
                    paper_position = {
                        "active": True,
                        "side": raw_signal,
                        "entry_price": price,
                        "qty": qty,
                        "amount_idr": amount,
                    }
                    msg = (f"{'[PAPER] ' if config.PAPER_TRADING else ''}ORDER EXECUTED\n"
                           f"{raw_signal} @ {price}\n"
                           f"Amount: {amount:.0f} IDR ({qty:.6f} coin)\n"
                           f"Signal: {signals.get('signal_reason')}\n"
                           f"LLM: {decision['reasoning']}")
                    await send_message(msg)
                    print("ORDER EXECUTED", flush=True)
                    return

            msg = (f"{'[PAPER] ' if config.PAPER_TRADING else ''}DECISION: {decision['decision']}\n"
                   f"Signal: {raw_signal} ({signals.get('signal_reason')})\n"
                   f"Price: {ticker.get('last')}\n"
                   f"LLM: {decision['reasoning'][:200]}")
            await send_message(msg)

    except Exception as e:
        print(f"Cycle error: {e}", flush=True)
        try:
            await send_message(f"Cycle error: {e}")
        except Exception:
            pass

async def main():
    global shutdown_flag

    print("Starting AI Trading Agent...", flush=True)
    print(f"  Pair: {config.PAIR}, Paper: {config.PAPER_TRADING}", flush=True)
    print(f"  DeepSeek model: {config.DEEPSEEK_MODEL}", flush=True)
    print(f"  Loop interval: {config.LOOP_INTERVAL_SECONDS}s", flush=True)
    print("-" * 40, flush=True)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        init_db()
        print("DB init OK", flush=True)
    except Exception as e:
        print(f"DB init FAILED: {e}", flush=True)

    ok = await send_message(f"Bot started — {config.PAIR}, paper={config.PAPER_TRADING}")
    print(f"Telegram notification: {'OK' if ok else 'FAILED'}", flush=True)

    async def telegram_poller():
        last_update_id = 0
        while not shutdown_flag:
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates",
                        json={"offset": last_update_id + 1, "timeout": 30},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        for upd in data.get("result", []):
                            last_update_id = upd["update_id"]
                            msg = upd.get("message", {})
                            text = (msg.get("text") or "").strip().lower()
                            chat_id = msg.get("chat", {}).get("id")
                            if text == "/start":
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": chat_id, "text": "🤖 Bot running!\n/status — cek kondisi"},
                                    )
                            elif text == "/status":
                                pos_text = (f"Active: {paper_position['side']} @ {paper_position['entry_price']}"
                                            if paper_position["active"] else "No position")
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": chat_id, "text": f"Pair: {config.PAIR}\nPaper: {config.PAPER_TRADING}\n{pos_text}"},
                                    )
            except Exception:
                pass
            await asyncio.sleep(5)

    async with httpx.AsyncClient(timeout=30) as client:
        poll_task = asyncio.create_task(telegram_poller())
        cycle_count = 0

        while not shutdown_flag:
            cycle_count += 1
            print(f"\n=== Cycle #{cycle_count} ===", flush=True)
            await cycle(client)
            for _ in range(config.LOOP_INTERVAL_SECONDS // 5):
                if shutdown_flag:
                    break
                await asyncio.sleep(5)

    poll_task.cancel()
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
