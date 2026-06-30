"""
AI Trading Agent — Indodax
Orchestrator: loop tiap N detik, evaluasi pasar & posisi,
eksekusi via LLM filter kalau ada sinyal.
"""

import asyncio
import sys
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

async def cycle(client: httpx.AsyncClient):
    try:
        print("Fetching ticker...", flush=True)
        ticker = await fetch_ticker(client)
        if not ticker:
            print("No ticker data", flush=True)
            return
        print(f"Ticker: last={ticker.get('last')}", flush=True)

        print("Fetching OHLCV...", flush=True)
        ohlcv = await fetch_ohlcv(client)
        print(f"OHLCV: {len(ohlcv)} bars", flush=True)

        ohlcv = await fetch_ohlcv(client)

        print("Computing signals...", flush=True)
        signals = compute_signals(ohlcv)
        raw_signal = signals["raw_signal"]
        print(f"Signal: {raw_signal} | RSI: {signals.get('rsi')} | Reason: {signals.get('signal_reason')}", flush=True)

        balances = None
        balance_idr = 100_000
        balance_coin = 0.0
        has_position = False
        entry_price = None

        if not config.PAPER_TRADING and config.INDODAX_API_KEY:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                pair = config.PAIR
                base, coin = pair.split("_")
                balance_idr = float(bal.get(base, 0))
                balance_coin = float(bal.get(coin, 0))
                orders = await get_open_orders(client)
                has_position = len(orders) > 0 or balance_coin > 0.001
            except Exception as e:
                await send_message(f"Balance fetch error: {e}")

        if risk.should_stop_trading(balance_idr):
            await send_message("Daily loss limit reached — bot stopped.")
            sys.exit(0)

        trade_history = get_recent_trades()

        decision = {"decision": "REJECT", "adjusted_size_pct": None, "reasoning": "No signal"}
        if raw_signal != "HOLD" and not has_position:
            decision = evaluate(signals, ticker, balance_idr, balance_coin,
                                has_position, entry_price, trade_history)

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
                    await send_message(
                        f"{'[PAPER] ' if config.PAPER_TRADING else ''}ORDER {raw_signal}\n"
                        f"Price: {price}\nAmount: {amount:.0f} IDR\n"
                        f"Signal: {raw_signal} ({signals.get('signal_reason')})\n"
                        f"LLM: {decision['reasoning']}"
                    )

        if has_position:
            if config.INDODAX_API_KEY:
                await refresh_deadman(client)

            sl_check = risk.check_sl_tp(entry_price or ticker["last"],
                                         ticker["last"], "BUY")
            if sl_check:
                await send_message(f"{sl_check} triggered at {ticker['last']}")

    except Exception as e:
        await send_message(f"Cycle error: {e}")

async def main():
    print("Starting AI Trading Agent...", flush=True)
    print(f"  Pair: {config.PAIR}, Paper: {config.PAPER_TRADING}", flush=True)
    print(f"  DeepSeek model: {config.DEEPSEEK_MODEL}", flush=True)
    print(f"  Loop interval: {config.LOOP_INTERVAL_SECONDS}s", flush=True)
    print("-" * 40, flush=True)

    try:
        init_db()
        print("DB init OK", flush=True)
    except Exception as e:
        print(f"DB init FAILED: {e}", flush=True)

    try:
        ok = await send_message(f"Bot started — {config.PAIR}, paper={config.PAPER_TRADING}")
        print(f"Telegram notification: {'OK' if ok else 'FAILED'}", flush=True)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

    async with httpx.AsyncClient(timeout=30) as client:
        cycle_count = 0
        while True:
            cycle_count += 1
            print(f"\n=== Cycle #{cycle_count} ===", flush=True)
            try:
                await cycle(client)
            except Exception as e:
                print(f"Fatal cycle error: {e}", flush=True)
                import traceback
                traceback.print_exc()
            print(f"Sleeping {config.LOOP_INTERVAL_SECONDS}s...", flush=True)
            await asyncio.sleep(config.LOOP_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    except Exception as e:
        print(f"Fatal: {e}", flush=True)
        import traceback
        traceback.print_exc()
