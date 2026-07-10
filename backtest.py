"""
Backtest Engine — Simulasi regime-gated + state machine.

Memutar ulang data OHLCV historis, menjalankan rules.decide() dan state machine
persis seperti bot live. Menghitung equity curve, win rate, Sharpe, drawdown.

Usage:
  python backtest.py --pairs btc_idr,eth_idr --days 30
  python backtest.py --days 90 --regime BULL
  python backtest.py --days 180 --walkforward --windows 6
  python backtest.py --days 360 --mc --mc_runs 2000
"""

import argparse, asyncio, random, math, sys, time
from datetime import datetime, timezone, timedelta
import numpy as np
import httpx
from data_layer import fetch_ohlcv
from indicators import compute_batch_signals
from hmm_regime import HMMRegimeDetector
import rules
import config

WIB = timezone(timedelta(hours=7))

FEE_BUY_MARKET = 0.0031
FEE_SELL_MAKER = 0.0020
TAX = 0.0021
CFX = 0.000111
FEE_ROUNDTRIP = FEE_BUY_MARKET + FEE_SELL_MAKER + TAX + CFX * 2


class SimState:
    """State machine untuk satu posisi — mirror SM live."""
    def __init__(self, pair, entry, qty, atr, mode, ts):
        self.pair = pair
        self.entry = entry
        self.qty = qty
        self.atr = atr
        self.mode = mode
        self.state = "TP_ACTIVE" if mode != "TRAILING" else "TRAILING"
        self.tp_price = int(entry * (1 + max(atr, 0.5) * 2.0 / 100))
        self.sl_price = int(entry * (1 - max(atr * 1.2, 0.8) / 100))
        self.trailing_high = entry
        self.ts_entry = ts
        self.ts_exit = 0
        self.exit_price = 0
        self.exit_reason = ""
        self.fee_buy = entry * qty * FEE_BUY_MARKET
        self.fee_sell = 0

    def tick(self, high, low, close, ts):
        if self.state == "CLOSED":
            return

        if self.state == "TP_ACTIVE":
            if high >= self.tp_price:
                self._close(self.tp_price, "TP", ts)
                return
            if low <= self.sl_price:
                self.state = "SL_ACTIVE"
                self.sl_price = int(self.entry * (1 - max(self.atr * 1.2, 0.8) / 100))

        elif self.state == "SL_ACTIVE":
            if low <= self.sl_price:
                self._close(self.sl_price, "SL", ts)
                return
            if close >= self.entry * 1.005:
                self.state = "TP_ACTIVE"

        elif self.state == "TRAILING":
            if close > self.trailing_high:
                self.trailing_high = close
            trail_sl = self.trailing_high * (1 - max(self.atr * 1.5, 0.8) / 100)
            if low <= trail_sl:
                self._close(trail_sl, "TRAILING_SL", ts)
                return

    def _close(self, price, reason, ts):
        self.exit_price = price
        self.exit_reason = reason
        self.state = "CLOSED"
        self.ts_exit = ts
        self.fee_sell = self.qty * price * (FEE_SELL_MAKER + TAX + CFX)

    def pnl(self):
        if self.exit_price == 0:
            return 0
        return (self.exit_price - self.entry) * self.qty - self.fee_buy - self.fee_sell


class Backtest:
    def __init__(self, capital=200000, regime_override=None):
        self.capital_initial = capital
        self.capital = capital
        self.positions = []
        self.states = {}
        self.trades = []
        self.equity_curve = []
        self.hmm = HMMRegimeDetector(n_states=4)
        self.hmm_trained = False
        self.hmm_train_counter = 0
        self.regime_override = regime_override
        self.daily_peak = capital
        self.cycle = 0

    def run(self, ohlcv_map):
        pairs = list(ohlcv_map.keys())
        min_len = min(len(o) for o in ohlcv_map.values())
        if min_len < 60:
            return self.capital

        self.equity_curve = [(0, self.capital)]

        for i in range(60, min_len):
            self.cycle += 1
            window = {p: o[:i] for p, o in ohlcv_map.items()}
            closes = {p: float(o[i-1]["close"]) for p, o in ohlcv_map.items()}
            highs = {p: float(o[i-1]["high"]) for p, o in ohlcv_map.items()}
            lows = {p: float(o[i-1]["low"]) for p, o in ohlcv_map.items()}
            ts = ohlcv_map[pairs[0]][i-1].get("Time", i)

            self._process_states(highs, lows, closes, ts)
            self._execute_decisions(window, pairs, closes, i)
            self._update_equity(closes, i)

        for p in list(self.positions):
            self._force_close(p, closes.get(p["pair"], 1000), "END")
        return self.capital

    def _process_states(self, highs, lows, closes, ts):
        for pid, st in list(self.states.items()):
            h = highs.get(pid, 0)
            lo = lows.get(pid, 0)
            c = closes.get(pid, 0)
            st.tick(h, lo, c, ts)
            if st.state == "CLOSED":
                pnl = st.pnl()
                self.capital += st.qty * st.exit_price - st.fee_sell
                self.trades.append({
                    "pair": pid, "entry": st.entry, "exit": st.exit_price,
                    "qty": st.qty, "pnl": pnl, "reason": st.exit_reason,
                    "ts_entry": st.ts_entry, "ts_exit": st.ts_exit,
                })
                self.positions = [x for x in self.positions if x["pair"] != pid]
                del self.states[pid]

    def _execute_decisions(self, window, pairs, closes, idx):
        sigs = compute_batch_signals(window, None)
        regime = self._get_regime(sigs, window)

        cash = self.capital
        tv = cash + sum(p["qty"] * closes.get(p["pair"], 1000) for p in self.positions)

        dec = rules.decide(
            sigs, {p: {"sell": closes[p], "vol_idr": 5e8} for p in pairs},
            {p: {"last": closes[p]} for p in pairs},
            self.positions, cash, tv, regime, None, set(), {},
        )

        for t in dec.get("trades", []):
            if t["action"] == "BUY" and len(self.positions) < 6:
                self._exec_buy(t, closes[t["pair"]], idx)
            elif t["action"] == "SELL":
                self._exec_sell(t, closes.get(t["pair"], 1000))

    def _exec_buy(self, t, price, idx):
        size = min(self.capital * 0.25, 50000)
        if size < 20000:
            return
        qty = size / price * 0.997
        fee = size * FEE_BUY_MARKET
        cost = size
        if self.capital < cost:
            return
        pair = t["pair"]
        self.capital -= cost
        self.positions.append({
            "pair": pair, "entry": price, "qty": qty, "size": cost, "ts": idx,
        })
        atr = 1.5
        for o in (window.get(pair, [])[-30:]):
            if isinstance(o, dict) and "atr_pct" in o:
                try:
                    atr = float(o["atr_pct"])
                except Exception:
                    pass
        mode = "TRAILING" if self.regime_override == "BULL" else "TP_ACTIVE"
        self.states[pair] = SimState(pair, price, qty, atr, mode, idx)

    def _exec_sell(self, t, price):
        p = next((x for x in self.positions if x["pair"] == t["pair"]), None)
        if not p:
            return
        self._force_close(p, price, t.get("reason", "SIGNAL"))

    def _force_close(self, p, price, reason):
        if p["pair"] in self.states:
            st = self.states[p["pair"]]
            st.exit_price = price
            st.exit_reason = reason
            st.state = "CLOSED"
            pnl = st.pnl()
            self.capital += p["qty"] * price - st.fee_sell
            self.trades.append({
                "pair": p["pair"], "entry": st.entry, "exit": price,
                "qty": p["qty"], "pnl": pnl, "reason": reason,
                "ts_entry": st.ts_entry, "ts_exit": self.cycle,
            })
        else:
            pnl = (price - p["entry"]) * p["qty"]
            fee = p["qty"] * price * (FEE_SELL_MAKER + TAX + CFX)
            self.capital += p["qty"] * price - fee
            self.trades.append({
                "pair": p["pair"], "entry": p["entry"], "exit": price,
                "qty": p["qty"], "pnl": pnl - fee, "reason": reason,
                "ts_entry": p["ts"], "ts_exit": self.cycle,
            })
        self.positions = [x for x in self.positions if x["pair"] != p["pair"]]

    def _update_equity(self, closes, idx):
        eq = self.capital + sum(p["qty"] * closes.get(p["pair"], 1000) for p in self.positions)
        self.equity_curve.append((idx, eq))
        if eq > self.daily_peak:
            self.daily_peak = eq

    def _get_regime(self, sigs, window):
        if self.regime_override:
            return {"regime": self.regime_override, "hmm_regime": self.regime_override, "hmm_confidence": 1.0,
                    "buy_ratio": 0.3, "sell_ratio": 0.1, "avg_score": 0.5, "high_conviction_count": 2, "avg_volatility": 1.5, "total_signals": 20}
        self.hmm_train_counter += 1
        if self.hmm_train_counter % 50 == 0 and not self.hmm_trained:
            try:
                self.hmm.fit(window)
                self.hmm_trained = self.hmm.trained
            except Exception:
                pass
        if self.hmm_trained:
            try:
                return self.hmm.predict(window)
            except Exception:
                pass
        return {"regime": "SIDEWAYS", "hmm_regime": "SIDEWAYS", "hmm_confidence": 0.0,
                "buy_ratio": 0.3, "sell_ratio": 0.1, "avg_score": 0.5, "high_conviction_count": 2, "avg_volatility": 1.5, "total_signals": 20}

    def report(self):
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in self.trades)
        gross_win = sum(t["pnl"] for t in wins) or 1
        gross_loss = abs(sum(t["pnl"] for t in losses)) or 1
        profit_factor = gross_win / gross_loss if gross_loss else float("inf")
        returns = [self.equity_curve[i][1] / self.equity_curve[i-1][1] - 1 for i in range(1, len(self.equity_curve))]
        sharpe = (np.mean(returns) / np.std(returns) * math.sqrt(365)) if len(returns) > 1 and np.std(returns) > 0 else 0
        peak = max(e for _, e in self.equity_curve)
        dd = (peak - min(e for _, e in self.equity_curve)) / peak * 100 if peak > 0 else 0

        print(f"\n{'='*55}")
        print(f"  BACKTEST RESULT")
        print(f"{'='*55}")
        print(f"  Initial capital: Rp{self.capital_initial:,.0f}")
        print(f"  Final capital:   Rp{self.capital:,.0f}")
        print(f"  Return:          {(self.capital/self.capital_initial-1)*100:+.2f}%")
        print(f"  Total trades:    {len(self.trades)}")
        print(f"  Wins:            {len(wins)}")
        print(f"  Losses:          {len(losses)}")
        print(f"  Win rate:        {len(wins)/len(self.trades)*100:.1f}%" if self.trades else "  Win rate: N/A")
        print(f"  Profit factor:   {profit_factor:.2f}")
        print(f"  Sharpe:          {sharpe:.2f}")
        print(f"  Max drawdown:    {dd:.1f}%")
        print(f"  Avg win:         Rp{np.mean([t['pnl'] for t in wins]):+,.0f}" if wins else "")
        print(f"  Avg loss:        Rp{np.mean([t['pnl'] for t in losses]):+,.0f}" if losses else "")
        print(f"  Largest win:     Rp{max(t['pnl'] for t in wins):+,.0f}" if wins else "")
        print(f"  Largest loss:    Rp{min(t['pnl'] for t in losses):+,.0f}" if losses else "")
        print(f"\n  Trade breakdown by reason:")
        reasons = {}
        for t in self.trades:
            r = t.get("reason", "unknown")[:15]
            reasons.setdefault(r, {"count": 0, "pnl": 0})
            reasons[r]["count"] += 1
            reasons[r]["pnl"] += t["pnl"]
        for r, v in sorted(reasons.items(), key=lambda x: -abs(x[1]["pnl"])):
            em = "🟢" if v["pnl"] > 0 else "🔴"
            print(f"    {em} {r:<15} {v['count']:>4}x  PnL: Rp{v['pnl']:>+8,.0f}")

    def walk_forward(self, ohlcv_map, windows=6):
        pairs = list(ohlcv_map.keys())
        min_len = min(len(o) for o in ohlcv_map.values())
        chunk = min_len // windows
        results = []
        for w in range(windows):
            test_map = {p: o[w*chunk:(w+1)*chunk] for p, o in ohlcv_map.items()}
            eng = Backtest(self.capital_initial, self.regime_override)
            final = eng.run(test_map)
            results.append({
                "window": w+1, "trades": len(eng.trades),
                "pnl": final - self.capital_initial,
                "return_pct": (final - self.capital_initial) / self.capital_initial * 100,
                "win_rate": sum(1 for t in eng.trades if t["pnl"] > 0) / len(eng.trades) * 100 if eng.trades else 0,
            })
        return results

    def monte_carlo(self, ohlcv_map, n_sims=1000):
        self.run(ohlcv_map)
        if not self.trades:
            return {"mean": 0, "median": 0, "std": 0, "q5": 0, "q95": 0, "prob_positive": 0}
        pnls = [t["pnl"] for t in self.trades]
        results = [sum(random.choices(pnls, k=len(pnls))) for _ in range(n_sims)]
        arr = np.array(results)
        return {
            "mean": float(np.mean(arr)),
            "median": float(np.median(arr)),
            "std": float(np.std(arr)),
            "q5": float(np.percentile(arr, 5)),
            "q95": float(np.percentile(arr, 95)),
            "prob_positive": float(np.mean(arr > 0)),
        }


async def main():
    parser = argparse.ArgumentParser(description="Backtest FMA Alpha Quant Labs")
    parser.add_argument("--pairs", default="btc_idr,sol_idr")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--tf", type=int, default=60)
    parser.add_argument("--capital", type=int, default=200000)
    parser.add_argument("--regime", default=None, choices=["BULL", "BEAR", "SIDEWAYS", "HIGH_VOL"])
    parser.add_argument("--walkforward", action="store_true")
    parser.add_argument("--windows", type=int, default=6)
    parser.add_argument("--mc", action="store_true")
    parser.add_argument("--mc_runs", type=int, default=2000)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=60) as c:
        ohlcv_map = {}
        for pair in args.pairs.split(","):
            print(f"Fetching {pair}...")
            ohlcv = await fetch_ohlcv(c, pair=pair.strip(), tf=args.tf, limit=args.days * 24)
            if len(ohlcv) >= 60:
                ohlcv_map[pair.strip()] = ohlcv
                print(f"  {len(ohlcv)} candles")
            else:
                print(f"  Insufficient data ({len(ohlcv)})")

    if not ohlcv_map:
        print("No data available.")
        return

    eng = Backtest(args.capital, args.regime)

    if args.walkforward:
        results = eng.walk_forward(ohlcv_map, args.windows)
        print(f"\n{'='*55}")
        print(f"  WALK-FORWARD ({args.windows} windows)")
        print(f"{'='*55}")
        for r in results:
            print(f"  Window {r['window']}: {r['trades']} trades | "
                  f"PnL Rp{r['pnl']:+,.0f} ({r['return_pct']:+.2f}%) | WR {r['win_rate']:.1f}%")
    elif args.mc:
        eng.run(ohlcv_map)
        mc = eng.monte_carlo(ohlcv_map, args.mc_runs)
        eng.report()
        print(f"\n  MONTE CARLO ({args.mc_runs} runs):")
        print(f"    Mean:     Rp{mc['mean']:+,.0f}")
        print(f"    Median:   Rp{mc['median']:+,.0f}")
        print(f"    Std:      Rp{mc['std']:,.0f}")
        print(f"    5% ile:   Rp{mc['q5']:+,.0f}")
        print(f"    95% ile:  Rp{mc['q95']:+,.0f}")
        print(f"    Prob pos: {mc['prob_positive']*100:.1f}%")
    else:
        final = eng.run(ohlcv_map)
        eng.report()

if __name__ == "__main__":
    asyncio.run(main())
