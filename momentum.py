import numpy as np
import pandas as pd
import config

class MomentumEngine:
    def __init__(self):
        self.last_signal: dict[str, str] = {}

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
        """ATR percentile: 0=low vol, 100=high vol relative to 20 periods"""
        vals = []
        for i in range(20, min(len(ohlcv), 50)):
            chunk = ohlcv[i-14:i]
            vals.append(self._compute_atr(chunk))
        if not vals:
            return 50
        current = self._compute_atr(ohlcv)
        rank = sum(1 for v in vals if v < current)
        return rank / len(vals) * 100

    def ema_crossover(self, closes: list[float], atr: float, atr_avg: float) -> bool:
        if len(closes) < 22:
            return False
        ema9 = pd.Series(closes).ewm(span=9).mean().iloc[-1]
        ema21 = pd.Series(closes).ewm(span=21).mean().iloc[-1]
        ema9_p = pd.Series(closes).ewm(span=9).mean().iloc[-2]
        ema21_p = pd.Series(closes).ewm(span=21).mean().iloc[-2]
        crossed = ema9_p < ema21_p and ema9 > ema21
        if not crossed:
            return False
        return atr > atr_avg * 0.8

    def volume_spike(self, ohlcv: list[dict], atr_pctile: float) -> bool:
        if len(ohlcv) < 21:
            return False
        vols = [float(c.get("volume", c.get("vol", 0))) for c in ohlcv[-21:]]
        current = vols[-1]
        avg = np.mean(vols[:-1])
        if avg <= 0:
            return False
        ratio = current / avg
        min_ratio = max(2.5 - atr_pctile / 100, 1.5)
        return ratio >= min_ratio

    def rsi_oversold(self, closes: list[float], atr: float) -> bool:
        if len(closes) < 15:
            return False
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_g = np.mean(gains[-14:]) if len(gains) >= 14 else 0
        avg_l = np.mean(losses[-14:]) if len(losses) >= 14 else 1
        rs = avg_g / max(avg_l, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        oversold_threshold = 30 + atr * 1.5
        return rsi < oversold_threshold

    def price_breakout(self, closes: list[float], current_price: float, atr: float) -> bool:
        if len(closes) < 6:
            return False
        high_1h = max(closes[-6:])
        prev_high = max(closes[-7:-1])
        min_break = high_1h * atr * 0.3 / 100
        return current_price > high_1h + min_break and high_1h >= prev_high

    def evaluate(self, pair: str, ohlcv_1h: list[dict], price: float) -> str | None:
        closes = [float(c["close"]) for c in ohlcv_1h[-60:]] if len(ohlcv_1h) >= 30 else []
        if len(closes) < 22:
            return None

        atr = self._compute_atr(ohlcv_1h)
        atr_pctile = self._atr_percentile(ohlcv_1h)
        atr_avg = np.mean([self._compute_atr(ohlcv_1h[i-14:i]) for i in range(20, min(len(ohlcv_1h), 50))]) if len(ohlcv_1h) > 30 else atr

        reasons = []
        if self.ema_crossover(closes, atr, atr_avg):
            reasons.append("EMA9/21")
        if self.volume_spike(ohlcv_1h, atr_pctile):
            reasons.append(f"VOL{atr_pctile:.0f}")
        if self.rsi_oversold(closes, atr):
            reasons.append(f"RSI{atr:.0f}")
        if self.price_breakout(closes, price, atr):
            reasons.append(f"BRK{atr:.1f}")

        min_signals = 1 if atr > 5 else 2
        if len(reasons) >= min_signals:
            return f"MOM:+{'+'.join(reasons)}"
        return None
