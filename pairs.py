# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import pandas as pd
import numpy as np
import config
from indicators import compute_signals


def compute_pair_signal(ohlcv_a, ohlcv_b, pair_name, window=50):
    if len(ohlcv_a) < window or len(ohlcv_b) < window:
        return {"pair": pair_name, "signal": "HOLD", "z_score": 0, "ratio": 0}

    df_a = pd.DataFrame(ohlcv_a)
    df_b = pd.DataFrame(ohlcv_b)
    close_a = df_a["close"].astype(float).values
    close_b = df_b["close"].astype(float).values

    min_len = min(len(close_a), len(close_b))
    ratio = close_a[-min_len:] / close_b[-min_len:]
    ma = pd.Series(ratio).rolling(window).mean().values
    std = pd.Series(ratio).rolling(window).std().values

    if std[-1] == 0 or np.isnan(std[-1]):
        return {"pair": pair_name, "signal": "HOLD", "z_score": 0, "ratio": round(ratio[-1], 4)}

    z = (ratio[-1] - ma[-1]) / std[-1]
    result = {
        "pair": pair_name, "z_score": round(z, 2), "ratio": round(ratio[-1], 4),
        "signal": "HOLD", "action_a": "", "action_b": "", "reason": "",
    }

    if z > 2:
        result["signal"] = "SHORT_SPREAD"
        result["action_a"] = "SELL"; result["action_b"] = "BUY"
        result["reason"] = f"A overvalued vs B (z={z:.2f})"
    elif z < -2:
        result["signal"] = "LONG_SPREAD"
        result["action_a"] = "BUY"; result["action_b"] = "SELL"
        result["reason"] = f"A undervalued vs B (z={z:.2f})"

    return result


def compute_all_pairs(ohlcv_map):
    signals = []
    for pair_a, pair_b in config.CORRELATION_PAIRS:
        ohlcv_a = ohlcv_map.get(pair_a, [])
        ohlcv_b = ohlcv_map.get(pair_b, [])
        if ohlcv_a and ohlcv_b:
            sig = compute_pair_signal(ohlcv_a, ohlcv_b, f"{pair_a}/{pair_b}")
            signals.append(sig)
    return signals


def pair_signals_to_trades(pair_signals, ticker_map, live_tickers):
    trades = []
    for ps in pair_signals:
        if ps["signal"] not in ("SHORT_SPREAD", "LONG_SPREAD"):
            continue
        pairs = ps["pair"].split("/")
        price_a = ticker_map.get(pairs[0], {}).get("sell") or live_tickers.get(pairs[0], {}).get("last", 0)
        price_b = ticker_map.get(pairs[1], {}).get("sell") or live_tickers.get(pairs[1], {}).get("last", 0)
        if price_a < 50 or price_b < 50:
            continue
        vol_a = float(ticker_map.get(pairs[0], {}).get("vol_idr", 0))
        vol_b = float(ticker_map.get(pairs[1], {}).get("vol_idr", 0))
        if vol_a < 200_000_000 or vol_b < 200_000_000:
            continue
        trades.append({
            "pair": pairs[0], "action": ps["action_a"], "allocation_pct": 15, "reason": ps["reason"],
        })
        trades.append({
            "pair": pairs[1], "action": ps["action_b"], "allocation_pct": 15, "reason": ps["reason"],
        })
    return trades
