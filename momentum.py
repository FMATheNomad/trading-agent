# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import numpy as np
import pandas as pd
import config

class MomentumEngine:
    def __init__(self):
        self.last_signal: dict[str, str] = {}
        self._confirmed: dict[str, str] = {}

    def _compute_atr(self, ohlcv: list[dict], period: int = 14) -> float:
        d = [c for c in ohlcv if isinstance(c, dict)][-(period+1):]
        if len(d) < period + 1:
            return 1.0
        trs = []
        for i in range(1, len(d)):
            h, l, pc = float(d[i].get("high", 0)), float(d[i].get("low", 0)), float(d[-1].get("close", 0))
            trs.append(max(h-l, abs(h-float(d[i-1].get("close",0))), abs(l-float(d[i-1].get("close",0)))))
        price = float(d[-1].get("close", 1))
        return (sum(trs) / len(trs) / price * 100) if price and trs else 1.0

    def _atr_percentile(self, ohlcv: list[dict]) -> float:
        vals = []
        for i in range(20, min(len(ohlcv), 50)):
            chunk = ohlcv[i-14:i]
            vals.append(self._compute_atr(chunk))
        if not vals:
            return 50
        current = self._compute_atr(ohlcv)
        rank = sum(1 for v in vals if v < current)
        return rank / len(vals) * 100

    def ema_crossover(self, closes: list[float], atr: float, atr_avg: float) -> tuple[bool, float]:
        if len(closes) < 22:
            return False, 0.0
        ema9 = pd.Series(closes).ewm(span=9).mean().iloc[-1]
        ema21 = pd.Series(closes).ewm(span=21).mean().iloc[-1]
        ema9_p = pd.Series(closes).ewm(span=9).mean().iloc[-2]
        ema21_p = pd.Series(closes).ewm(span=21).mean().iloc[-2]
        crossed = ema9_p < ema21_p and ema9 > ema21
        if not crossed:
            return False, 0.0
        velocity = abs(ema9 - ema21) / closes[-1] * 100
        return atr > atr_avg * 0.8, velocity

    def volume_spike(self, ohlcv: list[dict], atr_pctile: float) -> tuple[bool, float]:
        if len(ohlcv) < 21:
            return False, 0.0
        vols = [float(c.get("volume", c.get("vol", 0))) for c in ohlcv[-21:]]
        current = vols[-1]
        avg = np.mean(vols[:-1])
        if avg <= 0:
            return False, 0.0
        ratio = current / avg
        min_ratio = max(2.5 - atr_pctile / 100, 1.5)
        vel = (vols[-1] - vols[-5]) / max(vols[-5], 1) * 100 if len(vols) >= 5 else 0
        return ratio >= min_ratio, vel

    def rsi_oversold(self, closes: list[float], atr: float) -> tuple[bool, float]:
        if len(closes) < 15:
            return False, 0.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_g = np.mean(gains[-14:]) if len(gains) >= 14 else 0
        avg_l = np.mean(losses[-14:]) if len(losses) >= 14 else 1
        rs = avg_g / max(avg_l, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        oversold_threshold = 30 + atr * 1.5
        depth = (oversold_threshold - rsi) / oversold_threshold * 100 if rsi < oversold_threshold else 0
        return rsi < oversold_threshold, depth

    def evaluate(self, pair: str, ohlcv_1h: list[dict], price: float) -> str | None:
        closes = [float(c["close"]) for c in ohlcv_1h[-60:]] if len(ohlcv_1h) >= 30 else []
        if len(closes) < 22:
            return None

        atr = self._compute_atr(ohlcv_1h)
        atr_pctile = self._atr_percentile(ohlcv_1h)
        atr_avg = np.mean([self._compute_atr(ohlcv_1h[i-14:i]) for i in range(20, min(len(ohlcv_1h), 50))]) if len(ohlcv_1h) > 30 else atr

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_g = np.mean(gains[-14:]) if len(gains) >= 14 else 0
        avg_l = np.mean(losses[-14:]) if len(losses) >= 14 else 1
        rs = avg_g / max(avg_l, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        reasons = []
        vel_sum = 0.0
        n_vel = 0

        ok, vel = self.ema_crossover(closes, atr, atr_avg)
        if ok:
            reasons.append("EMA9/21")
            vel_sum += vel; n_vel += 1

        ok, vel = self.volume_spike(ohlcv_1h, atr_pctile)
        if ok:
            reasons.append(f"VOL{atr_pctile:.0f}")
            vel_sum += vel; n_vel += 1

        ok, vel = self.rsi_oversold(closes, atr)
        if ok:
            reasons.append(f"RSI{atr:.0f}")
            vel_sum += vel; n_vel += 1

        avg_vel = vel_sum / max(n_vel, 1)
        sig_vel_tag = f"V{avg_vel:.0f}" if avg_vel > 0 else ""

        signal_key = "+".join(sorted(reasons)) if reasons else None
        if signal_key and len(reasons) >= 2:
            if self._confirmed.get(pair) == signal_key:
                self._confirmed.pop(pair, None)
                return f"MOM:+{signal_key}+{sig_vel_tag}"
            self._confirmed[pair] = signal_key
        else:
            self._confirmed.pop(pair, None)
        return None
