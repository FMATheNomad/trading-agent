# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""
Paradiddle Pattern Detection for crypto price action.

Paradiddle adalah pola pukulan drummer: R L R R L R L L.
Dalam trading, harga bergerak dalam pola R (Resistance touch) dan L (Low touch)
yang bisa diprediksi. Detektor ini mengenali pola tersebut untuk entry/exit signals.
"""

def detect_paradiddle(closes: list[float], n_bars: int = 8) -> str | None:
    if len(closes) < n_bars + 2:
        return None
    recent = closes[-(n_bars + 2):]
    pattern_chars = []
    for i in range(2, len(recent) - 2):
        left = recent[i - 1]
        cur = recent[i]
        right = recent[i + 1]
        avg_range = (max(recent[i-2:i+3]) - min(recent[i-2:i+3])) or 1
        threshold = avg_range * 0.15
        if cur > left + threshold and cur > right + threshold:
            pattern_chars.append("R")
        elif cur < left - threshold and cur < right - threshold:
            pattern_chars.append("L")
    s = "".join(pattern_chars[-6:]) if len(pattern_chars) >= 2 else ""
    if "RRR" in s and "L" not in s[-3:]:
        return None
    if "LLL" in s and "R" not in s[-3:]:
        return None
    end3 = s[-3:] if len(s) >= 3 else s
    if end3 in ("RLR", "RLRR") and s.count("R") >= s.count("L"):
        return "FAKE_BREAKOUT_SELL"
    if end3 in ("LRL", "LRLL") and s.count("L") >= s.count("R"):
        return "FAKE_BREAKDOWN_BUY"
    if end3 == "RRL" and s[-4:-1] == "RRR":
        return "EXHAUSTION_SELL"
    if end3 == "LLR" and s[-4:-1] == "LLL":
        return "EXHAUSTION_BUY"
    return None


def compute_micro_momentum(closes: list[float], vols: list[float]) -> dict:
    if len(closes) < 10:
        return {"momentum": 0, "acceleration": 0, "volume_velocity": 0}
    price_vel = (closes[-1] - closes[-3]) / max(closes[-3], 1) * 100
    price_accel = price_vel - ((closes[-3] - closes[-6]) / max(closes[-6], 1) * 100) if len(closes) >= 6 else 0
    vol_vel = (vols[-1] - vols[-3]) / max(vols[-3], 1) * 100 if len(vols) >= 3 else 0
    return {
        "momentum": round(price_vel, 2),
        "acceleration": round(price_accel, 2),
        "volume_velocity": round(vol_vel, 2),
    }
