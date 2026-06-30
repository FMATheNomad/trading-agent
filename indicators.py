import pandas as pd
import ta

def _compute_single_tf(ohlcv: list[dict]) -> dict:
    if len(ohlcv) < 30:
        return {}

    df = pd.DataFrame(ohlcv)
    df.columns = [c.lower() for c in df.columns]
    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    volumes = df.get("volume", df.get("vol", None))
    if volumes is not None:
        volumes = volumes.astype(float)

    rsi = ta.momentum.RSIIndicator(closes, window=14).rsi()
    ema9 = ta.trend.EMAIndicator(closes, window=9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(closes, window=21).ema_indicator()
    ema50 = ta.trend.EMAIndicator(closes, window=50).ema_indicator()
    macd = ta.trend.MACD(closes)
    bb = ta.volatility.BollingerBands(closes, window=20, window_dev=2)

    rsi_val = rsi.iloc[-1]
    ema9_val = ema9.iloc[-1]
    ema21_val = ema21.iloc[-1]
    ema50_val = ema50.iloc[-1]
    macd_line = macd.macd().iloc[-1]
    macd_signal = macd.macd_signal().iloc[-1]
    macd_hist = macd.macd_diff().iloc[-1]
    bb_lower = bb.bollinger_lband().iloc[-1]
    bb_upper = bb.bollinger_hband().iloc[-1]
    bb_width = bb.bollinger_wband().iloc[-1]
    last_price = closes.iloc[-1]
    prev_price = closes.iloc[-2] if len(closes) > 1 else last_price

    vol_current = float(volumes.iloc[-1]) if volumes is not None else 0
    vol_avg = float(volumes.tail(20).mean()) if volumes is not None else 1
    vol_ratio = round(vol_current / vol_avg, 2) if vol_avg > 0 else 1

    closes_arr = closes.values
    streak_val, streak_dir = 0, "flat"
    direction = None
    for i in range(min(5, len(closes_arr) - 1)):
        if closes_arr[-(i + 1)] > closes_arr[-(i + 2)]:
            if direction is None or direction == 1:
                direction = 1; streak_val += 1
            else: break
        elif closes_arr[-(i + 1)] < closes_arr[-(i + 2)]:
            if direction is None or direction == -1:
                direction = -1; streak_val -= 1
            else: break
        else: break
    streak_dir = "up" if streak_val > 0 else "down" if streak_val < 0 else "flat"

    vol_pct = round(closes.pct_change().std() * 100, 2)
    price_range_14 = closes.tail(14)
    range_pct = round((price_range_14.max() - price_range_14.min()) / price_range_14.min() * 100, 2) if price_range_14.min() > 0 else 0

    return {
        "rsi": round(rsi_val, 2) if pd.notna(rsi_val) else None,
        "ema9": round(ema9_val, 2) if pd.notna(ema9_val) else None,
        "ema21": round(ema21_val, 2) if pd.notna(ema21_val) else None,
        "ema50": round(ema50_val, 2) if pd.notna(ema50_val) else None,
        "macd_line": round(macd_line, 8) if pd.notna(macd_line) else None,
        "macd_signal": round(macd_signal, 8) if pd.notna(macd_signal) else None,
        "macd_hist": round(macd_hist, 8) if pd.notna(macd_hist) else None,
        "bb_lower": round(bb_lower, 2) if pd.notna(bb_lower) else None,
        "bb_upper": round(bb_upper, 2) if pd.notna(bb_upper) else None,
        "bb_width": round(bb_width, 2) if pd.notna(bb_width) else None,
        "last_price": round(last_price, 2),
        "price_change_pct": round((last_price - prev_price) / prev_price * 100, 2) if prev_price else 0,
        "volatility": vol_pct,
        "range_14_pct": range_pct,
        "volume_ratio": vol_ratio,
        "momentum_streak": streak_val,
        "momentum_dir": streak_dir,
    }


def _score_signal(ind: dict) -> tuple[str, str, int]:
    score = 0
    reasons = []

    if ind.get("rsi") is not None:
        if ind["rsi"] < 35:
            score += 2; reasons.append("rsi_oversold")
        elif ind["rsi"] < 45:
            score += 1; reasons.append("rsi_low")
        elif ind["rsi"] > 65:
            score -= 2; reasons.append("rsi_overbought")
        elif ind["rsi"] > 55:
            score -= 1; reasons.append("rsi_high")

    if ind.get("ema9") and ind.get("ema21"):
        if ind["ema9"] > ind["ema21"]:
            score += 1; reasons.append("ema_bullish")
        else:
            score -= 1; reasons.append("ema_bearish")

    if ind.get("ema21") and ind.get("ema50"):
        if ind["ema21"] > ind["ema50"]:
            score += 1; reasons.append("trend_up")
        else:
            score -= 1; reasons.append("trend_down")

    if ind.get("macd_line") is not None and ind.get("macd_signal") is not None:
        if ind["macd_line"] > ind["macd_signal"]:
            score += 1
            if ind.get("macd_hist", 0) > 0:
                score += 1; reasons.append("macd_momentum_up")
            reasons.append("macd_bullish")
        else:
            score -= 1
            if ind.get("macd_hist", 0) < 0:
                score -= 1; reasons.append("macd_momentum_down")
            reasons.append("macd_bearish")

    if ind.get("bb_lower") and ind.get("last_price"):
        if ind["last_price"] <= ind["bb_lower"]:
            score += 1; reasons.append("bb_support")
    if ind.get("bb_upper") and ind.get("last_price"):
        if ind["last_price"] >= ind["bb_upper"]:
            score -= 1; reasons.append("bb_resistance")

    vol = ind.get("volume_ratio", 1)
    if vol > 2:
        if score > 0: score += 1; reasons.append("vol_breakout")
        elif score < 0: score -= 1; reasons.append("vol_dump")

    streak = ind.get("momentum_streak", 0)
    if streak >= 3: score += 1; reasons.append(f"streak_{streak}")
    elif streak <= -3: score -= 1; reasons.append(f"streak_{streak}")

    if score >= 4:
        return ("BUY", "; ".join(reasons), score)
    elif score <= -3:
        return ("SELL", "; ".join(reasons), score)
    return ("HOLD", " | ".join(reasons) if reasons else "neutral", score)


def compute_signals(ohlcv_map: dict[str, list[dict]], tf_label: str = "1h") -> dict[str, dict]:
    result = {}
    for pair, ohlcv in ohlcv_map.items():
        try:
            raw = _compute_single_tf(ohlcv)
            if not raw:
                result[pair] = {"raw_signal": "HOLD", "score": 0, "reason": "insufficient_data", "tf": tf_label}
                continue
            sig, reason, score = _score_signal(raw)
            raw["raw_signal"] = sig
            raw["signal_reason"] = reason
            raw["score"] = score
            raw["tf"] = tf_label
            result[pair] = raw
        except Exception as e:
            result[pair] = {"raw_signal": "HOLD", "score": 0, "reason": f"error: {e}", "tf": tf_label}
    return result


def compute_single(ohlcv: list[dict]) -> dict:
    raw = _compute_single_tf(ohlcv)
    if raw:
        sig, reason, score = _score_signal(raw)
        raw["raw_signal"] = sig
        raw["signal_reason"] = reason
        raw["score"] = score
    return raw or {"raw_signal": "HOLD", "score": 0, "reason": "insufficient_data"}

def compute_batch_signals(ohlcv_map_1h: dict[str, list[dict]],
                           ohlcv_map_4h: dict[str, list[dict]] | None = None) -> dict[str, dict]:
    sigs_1h = compute_signals(ohlcv_map_1h, "1h")
    sigs_4h = compute_signals(ohlcv_map_4h, "4h") if ohlcv_map_4h else {}

    merged = {}
    all_pairs = set(sigs_1h.keys()) | set(sigs_4h.keys())
    for pair in all_pairs:
        s1 = sigs_1h.get(pair, {})
        s4 = sigs_4h.get(pair, {})
        combined = dict(s1)
        combined["4h_signal"] = s4.get("raw_signal", "N/A")
        combined["4h_score"] = s4.get("score", 0)
        combined["4h_reason"] = s4.get("signal_reason", "")

        tf_aligned = (s1.get("raw_signal") == s4.get("raw_signal") and s1.get("raw_signal") != "HOLD")
        combined["timeframe_aligned"] = tf_aligned
        combined["conviction"] = "HIGH" if tf_aligned else "LOW"

        merged[pair] = combined
    return merged
