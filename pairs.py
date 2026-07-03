# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import pandas as pd
import numpy as np
import config

def compute_pair_signal(ohlcv_a: list[dict], ohlcv_b: list[dict],
                         pair_name: str, window: int = 50) -> dict:
    if len(ohlcv_a) < window or len(ohlcv_b) < window:
        return {"pair": pair_name, "signal": "HOLD", "z_score": 0, "ratio": 0}

    df_a = pd.DataFrame(ohlcv_a)
    df_b = pd.DataFrame(ohlcv_b)
    close_a = df_a["close"].astype(float).values
    close_b = df_b["close"].astype(float).values

    min_len = min(len(close_a), len(close_b))
    close_a = close_a[-min_len:]
    close_b = close_b[-min_len:]

    ratio = close_a / close_b
    ma = pd.Series(ratio).rolling(window).mean().values
    std = pd.Series(ratio).rolling(window).std().values

    current_ratio = ratio[-1]
    current_ma = ma[-1]
    current_std = std[-1]

    if current_std == 0 or np.isnan(current_std):
        return {"pair": pair_name, "signal": "HOLD", "z_score": 0, "ratio": round(current_ratio, 4)}

    z = (current_ratio - current_ma) / current_std

    result = {
        "pair": pair_name,
        "z_score": round(z, 2),
        "ratio": round(current_ratio, 4),
        "mean_ratio": round(current_ma, 4),
        "std": round(current_std, 4),
        "signal": "HOLD",
        "action_a": "",
        "action_b": "",
        "reason": "",
    }

    if z > 2:
        result["signal"] = "SHORT_SPREAD"
        result["action_a"] = "SELL"
        result["action_b"] = "BUY"
        result["reason"] = f"A overvalued vs B (z={z:.2f})"
    elif z < -2:
        result["signal"] = "LONG_SPREAD"
        result["action_a"] = "BUY"
        result["action_b"] = "SELL"
        result["reason"] = f"A undervalued vs B (z={z:.2f})"
    elif z > 1.5:
        result["signal"] = "WATCH_SHORT"
        result["reason"] = f"A rich vs B, approaching threshold (z={z:.2f})"
    elif z < -1.5:
        result["signal"] = "WATCH_LONG"
        result["reason"] = f"A cheap vs B, approaching threshold (z={z:.2f})"

    return result


def compute_all_pairs(ohlcv_map: dict[str, list[dict]]) -> list[dict]:
    signals = []
    for pair_a, pair_b in config.CORRELATION_PAIRS:
        ohlcv_a = ohlcv_map.get(pair_a, [])
        ohlcv_b = ohlcv_map.get(pair_b, [])
        if ohlcv_a and ohlcv_b:
            sig = compute_pair_signal(ohlcv_a, ohlcv_b, f"{pair_a}/{pair_b}")
            signals.append(sig)
    return signals
