# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Backtest module — Simulasi regime-gated + state machine.

Usage:
  python backtest.py --days 30
  python backtest.py --days 60 --regime BULL
  python backtest.py --days 90 --walkforward --windows 5
"""

import argparse
import asyncio
import random
import numpy as np
import httpx
from data_layer import fetch_ohlcv
from indicators import compute_batch_signals
from hmm_regime import HMMRegimeDetector
from risk_manager import RiskManager
import rules
import config

FEE_BUY = 0.0031
FEE_SELL = 0.0020
TAX = 0.0021
CFX = 0.000111

class BacktestEngine:
    def __init__(self, capital=200_000):
        self.capital = capital
        self.positions = []
        self.trades = []
        self.hmm = HMMRegimeDetector(n_states=4)
        self.rm = RiskManager()

    def run(self, ohlcv_map, regime_override=None):
        pairs = list(ohlcv_map.keys())
        for i in range(60, len(ohlcv_map[pairs[0]])):
            window = {p: o[:i] for p in ohlcv_map for o in [ohlcv_map[p]]}
            sigs = compute_batch_signals(window, None)
            regime = self._get_regime(sigs, window, regime_override, i)
            dec = rules.decide(sigs, {}, {}, self.positions, self.capital, self.capital, regime, None, set(), {}, None)
            self._process_tp_sl()
            for t in dec.get("trades", []):
                if t["action"] == "BUY":
                    self._execute_buy(t, window)
                elif t["action"] == "SELL":
                    self._execute_sell(t)

        return self.capital + sum(p["qty"] * self._last_price(p["pair"]) for p in self.positions)

    def _get_regime(self, sigs, window, override, idx):
        if override:
            return {"regime": override, "hmm_regime": override, "hmm_confidence": 1.0}
        if idx % 50 == 0:
            try:
                self.hmm.fit(window)
            except Exception:
                pass
        return self.hmm.predict(window) if self.hmm.trained else {"regime": "SIDEWAYS"}

    def _last_price(self, pair):
        return 0

    def _execute_buy(self, t, window):
        size = min(self.capital * 0.25, 50000)
        if size < 20000:
            return
        qty = size / 1000
        fee = size * FEE_BUY
        self.positions.append({
            "pair": t["pair"], "entry": 1000, "qty": qty, "size": size, "fee": fee, "ts": 0,
        })
        self.capital -= size

    def _execute_sell(self, t):
        p = next((x for x in self.positions if x["pair"] == t["pair"]), None)
        if not p:
            return
        price = 1000
        pnl = (price - p["entry"]) * p["qty"]
        fee = price * p["qty"] * (FEE_SELL + TAX + CFX)
        self.capital += price * p["qty"] - fee
        self.trades.append({**p, "pnl": pnl - fee, "reason": t.get("reason", "")})
        self.positions.remove(p)

    def _process_tp_sl(self):
        for p in list(self.positions):
            price = 1000
            pnl_pct = (price - p["entry"]) / p["entry"]
            sl = p["entry"] * 0.985
            tp = p["entry"] * 1.02
            if price <= sl or price >= tp:
                pnl = (price - p["entry"]) * p["qty"]
                fee = price * p["qty"] * (FEE_SELL + TAX + CFX)
                self.capital += price * p["qty"] - fee
                reason = "SL" if price <= sl else "TP"
                self.trades.append({**p, "pnl": pnl - fee, "reason": reason})
                self.positions.remove(p)

    def report(self):
        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        wins = len([t for t in self.trades if t.get("pnl", 0) > 0])
        losses = len([t for t in self.trades if t.get("pnl", 0) <= 0])
        print(f"Trades: {len(self.trades)}")
        print(f"Wins: {wins} / Losses: {losses}")
        print(f"Win rate: {wins / len(self.trades) * 100:.1f}%" if self.trades else "N/A")
        print(f"Total PnL: {total_pnl:.2f}")
        print(f"Final capital: {self.capital:.2f}")

    def walk_forward(self, ohlcv_map, windows=5):
        pair0 = list(ohlcv_map.keys())[0]
        total = len(ohlcv_map[pair0])
        chunk = total // windows
        results = []
        for w in range(windows):
            test_map = {p: o[w*chunk:(w+1)*chunk] for p, o in ohlcv_map.items()}
            eng = BacktestEngine(self.capital)
            final = eng.run(test_map)
            results.append({
                "window": w+1, "trades": len(eng.trades),
                "pnl": final - self.capital,
                "return_pct": (final - self.capital) / self.capital * 100,
                "win_rate": sum(1 for t in eng.trades if t.get("pnl",0) > 0) / len(eng.trades) * 100 if eng.trades else 0,
            })
        return results

    def monte_carlo(self, ohlcv_map, n_sims=1000):
        self.run(ohlcv_map)
        if not self.trades:
            return []
        pnls = [t["pnl"] for t in self.trades]
        results = [sum(random.choices(pnls, k=len(pnls))) for _ in range(n_sims)]
        arr = np.array(results)
        return {
            "mean": float(np.mean(arr)), "median": float(np.median(arr)),
            "std": float(np.std(arr)), "q5": float(np.percentile(arr, 5)),
            "q95": float(np.percentile(arr, 95)),
            "prob_positive": float(np.mean(arr > 0)),
        }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--tf", type=int, default=60)
    parser.add_argument("--regime", default=None)
    parser.add_argument("--walkforward", action="store_true")
    parser.add_argument("--windows", type=int, default=5)
    parser.add_argument("--mc", action="store_true")
    parser.add_argument("--mc_runs", type=int, default=1000)
    args = parser.parse_args()

    config.PAIR = "btc_idr"
    async with httpx.AsyncClient() as c:
        ohlcv = await fetch_ohlcv(c, tf=args.tf, limit=args.days * 24)
    ohlcv_map = {config.PAIR: ohlcv} if len(ohlcv) >= 60 else {}
    if not ohlcv_map:
        print("Insufficient OHLCV data")
        return

    engine = BacktestEngine()
    if args.walkforward:
        results = engine.walk_forward(ohlcv_map, args.windows)
        for r in results:
            print(f"Window {r['window']}: {r['trades']} trades | PnL {r['pnl']:+.2f} ({r['return_pct']:+.2f}%) | WR {r['win_rate']:.1f}%")
    elif args.mc:
        engine.run(ohlcv_map, args.regime)
        mc = engine.monte_carlo(ohlcv_map, args.mc_runs)
        print(f"MC: mean={mc['mean']:+.2f} median={mc['median']:+.2f} prob_pos={mc['prob_positive']*100:.1f}%")
        engine.report()
    else:
        final = engine.run(ohlcv_map, args.regime)
        engine.report()
        print(f"Return: {(final - 200000) / 200000 * 100:.2f}%")

if __name__ == "__main__":
    asyncio.run(main())
