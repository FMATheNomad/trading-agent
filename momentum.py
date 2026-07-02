import numpy as np
import pandas as pd
import config

class MomentumEngine:
    def __init__(self):
        self.last_signal: dict[str, str] = {}

    def ema_crossover(self, closes: list[float]) -> bool:
        if len(closes) < 22:
            return False
        ema9 = pd.Series(closes).ewm(span=9).mean().iloc[-1]
        ema21 = pd.Series(closes).ewm(span=21).mean().iloc[-1]
        ema9_p = pd.Series(closes).ewm(span=9).mean().iloc[-2]
        ema21_p = pd.Series(closes).ewm(span=21).mean().iloc[-2]
        return ema9_p < ema21_p and ema9 > ema21

    def volume_spike(self, ohlcv: list[dict], min_ratio: float = 3.0) -> bool:
        if len(ohlcv) < 21:
            return False
        vols = [float(c.get("volume", c.get("vol", 0))) for c in ohlcv[-21:]]
        current = vols[-1]
        avg = np.mean(vols[:-1])
        return current > avg * min_ratio if avg > 0 else False

    def rsi_oversold(self, closes: list[float]) -> bool:
        if len(closes) < 15:
            return False
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_g = np.mean(gains[-14:]) if len(gains) >= 14 else 0
        avg_l = np.mean(losses[-14:]) if len(losses) >= 14 else 1
        rs = avg_g / max(avg_l, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi < 35

    def price_breakout(self, closes: list[float], current_price: float) -> bool:
        if len(closes) < 6:
            return False
        high_1h = max(closes[-6:])
        prev_high = max(closes[-7:-1])
        return current_price > high_1h and high_1h > prev_high

    def evaluate(self, pair: str, ohlcv_1h: list[dict], price: float) -> str | None:
        closes = [float(c["close"]) for c in ohlcv_1h[-60:]] if len(ohlcv_1h) >= 30 else []
        if len(closes) < 22:
            return None

        reasons = []
        if self.ema_crossover(closes):
            reasons.append("EMA9/21")
        if self.volume_spike(ohlcv_1h):
            reasons.append("VOLx3")
        if self.rsi_oversold(closes):
            reasons.append("RSI<35")
        if self.price_breakout(closes, price):
            reasons.append("BREAK")

        if len(reasons) >= 2:
            return f"MOMENTUM:+{'+'.join(reasons)}"
        return None
