import numpy as np
import config

class MomentumEngine:
    def __init__(self):
        self._confirmed: dict[str, str] = {}

    def evaluate(self, pair: str, ohlcv_1h: list[dict], price: float) -> str | None:
        closes = [float(c["close"]) for c in ohlcv_1h[-30:]] if len(ohlcv_1h) >= 15 else []
        if len(closes) < 10:
            return None

        vols = [float(c.get("volume", c.get("vol", 0))) for c in ohlcv_1h[-15:]]
        opens = [float(c["open"]) for c in ohlcv_1h[-10:]]

        vol_short = np.mean(vols[-3:]) if len(vols) >= 3 else 0
        vol_long = np.mean(vols[:-3]) if len(vols) > 3 else 0
        vol_ratio = vol_short / max(vol_long, 1)

        price_vel = (closes[-1] - closes[-3]) / max(closes[-3], 1) * 100 if len(closes) >= 3 else 0
        price_vel_prev = (closes[-3] - closes[-6]) / max(closes[-6], 1) * 100 if len(closes) >= 6 else 0
        acceleration = price_vel - price_vel_prev

        green = sum(1 for i in range(min(5, len(closes))) if closes[-(i+1)] > opens[-(i+1) if len(opens) >= i+1 else -1])
        
        reasons = []
        if vol_ratio >= 1.5:
            reasons.append(f"VOL{vol_ratio:.1f}x")
        if price_vel > 0.3 and acceleration > 0:
            reasons.append(f"ACC{acceleration:.1f}")
        if green >= 3:
            reasons.append(f"TREND{green}")

        signal_key = "+".join(sorted(reasons)) if len(reasons) >= 2 else None
        if signal_key:
            if self._confirmed.get(pair) == signal_key:
                self._confirmed.pop(pair, None)
                return f"MOM:+{signal_key}"
            self._confirmed[pair] = signal_key
        else:
            self._confirmed.pop(pair, None)
        return None
