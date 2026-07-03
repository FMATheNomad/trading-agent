# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""
Backtest module — Walk-forward analysis with Monte Carlo simulation.

Usage:
  python backtest.py --pair btc_idr --days 30 --tf 15
  python backtest.py --pair btc_idr --days 60 --walkforward --windows 5
  python backtest.py --pair btc_idr --days 90 --mc --mc_runs 1000
"""

import argparse
import asyncio
import random
import numpy as np
import httpx
from data_layer import fetch_ohlcv
from indicators import compute_single
from risk_manager import RiskManager
import config

class BacktestEngine:
    def __init__(self, initial_capital: float = 200_000):
        self.capital = initial_capital
        self.coin_balance = 0.0
        self.position = None
        self.rm = RiskManager()
        self.trades = []

    def run(self, ohlcv: list[dict]):
        for i in range(30, len(ohlcv)):
            window = ohlcv[:i]
            sig = compute_single(window)
            price = float(ohlcv[i - 1]["close"])

            if self.position:
                result = self.rm.check_sl_tp(self.position["price"], price, self.position["side"])
                if result:
                    pnl = (price - self.position["price"]) * self.position["qty"]
                    self.capital += self.position["qty"] * price
                    self.coin_balance = 0
                    self.trades.append({**self.position, "exit_price": price, "pnl": pnl, "reason": result})
                    self.position = None
                    continue

            if self.position is None and sig["raw_signal"] in ("BUY", "SELL"):
                size = self.rm.compute_position_size(self.capital)
                qty = size / price
                fee = self.rm.estimate_fee(size)
                if not self.rm.is_profit_viable(price, qty, sig["raw_signal"]):
                    continue
                self.position = {
                    "side": sig["raw_signal"],
                    "price": price,
                    "qty": qty,
                    "size": size,
                    "fee": fee,
                }
                self.coin_balance = qty
                self.capital -= size

    def walk_forward(self, ohlcv: list[dict], n_windows: int = 5):
        chunk_size = len(ohlcv) // n_windows
        results = []
        for w in range(n_windows):
            train_end = (w + 1) * chunk_size
            if train_end >= len(ohlcv):
                break
            test_start = train_end
            test_end = min(test_start + chunk_size, len(ohlcv))
            if test_end - test_start < 50:
                continue
            test_data = ohlcv[test_start:test_end]
            engine = BacktestEngine(self.capital)
            engine.run(test_data)
            results.append({
                "window": w + 1,
                "trades": len(engine.trades),
                "pnl": engine.capital - self.capital,
                "return_pct": (engine.capital - self.capital) / self.capital * 100,
                "win_rate": sum(1 for t in engine.trades if t.get("pnl", 0) > 0) / len(engine.trades) * 100 if engine.trades else 0,
            })
        return results

    def monte_carlo(self, ohlcv: list[dict], n_sims: int = 1000):
        self.run(ohlcv)
        if not self.trades:
            return []
        trade_pnls = [t.get("pnl", 0) for t in self.trades]
        results = []
        for _ in range(n_sims):
            sampled = random.choices(trade_pnls, k=len(trade_pnls))
            total = sum(sampled)
            results.append(total)
        arr = np.array(results)
        return {
            "mean": float(np.mean(arr)),
            "median": float(np.median(arr)),
            "std": float(np.std(arr)),
            "q5": float(np.percentile(arr, 5)),
            "q95": float(np.percentile(arr, 95)),
            "prob_positive": float(np.mean(arr > 0)),
        }

    def report(self):
        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        wins = len([t for t in self.trades if t.get("pnl", 0) > 0])
        losses = len([t for t in self.trades if t.get("pnl", 0) <= 0])
        print(f"Total trades: {len(self.trades)}")
        print(f"Wins: {wins} / Losses: {losses}")
        print(f"Win rate: {wins / len(self.trades) * 100:.1f}%" if self.trades else "N/A")
        print(f"Total PnL: {total_pnl:.2f}")
        print(f"Final capital: {self.capital:.2f}")
        print(f"Return: {(self.capital - 200_000) / 200_000 * 100:.2f}%")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="btc_idr")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--tf", type=int, default=15)
    parser.add_argument("--walkforward", action="store_true")
    parser.add_argument("--windows", type=int, default=5)
    parser.add_argument("--mc", action="store_true")
    parser.add_argument("--mc_runs", type=int, default=1000)
    args = parser.parse_args()

    config.PAIR = args.pair
    config.SYMBOL = args.pair.replace("_", "").upper()

    async with httpx.AsyncClient() as client:
        ohlcv = await fetch_ohlcv(client, tf=args.tf, limit=args.days * 24 * 4)

    engine = BacktestEngine()
    if args.walkforward:
        results = engine.walk_forward(ohlcv, n_windows=args.windows)
        print(f"\n=== Walk-Forward ({args.windows} windows) ===")
        for r in results:
            print(f"  Window {r['window']}: {r['trades']} trades | "
                  f"PnL: {r['pnl']:+.2f} ({r['return_pct']:+.2f}%) | "
                  f"WinRate: {r['win_rate']:.1f}%")
        avg_pnl = np.mean([r['pnl'] for r in results])
        avg_return = np.mean([r['return_pct'] for r in results])
        print(f"  Average: PnL={avg_pnl:+.2f} ({avg_return:+.2f}%)")
    elif args.mc:
        engine.run(ohlcv)
        mc = engine.monte_carlo(ohlcv, n_sims=args.mc_runs)
        print(f"\n=== Monte Carlo ({args.mc_runs} runs) ===")
        print(f"  Mean: {mc['mean']:+.2f}")
        print(f"  Median: {mc['median']:+.2f}")
        print(f"  Std: {mc['std']:.2f}")
        print(f"  5%ile: {mc['q5']:+.2f} | 95%ile: {mc['q95']:+.2f}")
        print(f"  Prob positive: {mc['prob_positive']*100:.1f}%")
        engine.report()
    else:
        engine.run(ohlcv)
        engine.report()

if __name__ == "__main__":
    asyncio.run(main())
