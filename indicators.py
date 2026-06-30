import pandas as pd
import ta

def compute_signals(ohlcv: list[dict]) -> dict:
    if len(ohlcv) < 30:
        return {"raw_signal": "HOLD", "reason": "insufficient_data"}

    df = pd.DataFrame(ohlcv)
    df.columns = [c.lower() for c in df.columns]
    closes = df["close"].astype(float)

    rsi = ta.momentum.RSIIndicator(closes, window=14).rsi()
    ema9 = ta.trend.EMAIndicator(closes, window=9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(closes, window=21).ema_indicator()
    macd = ta.trend.MACD(closes)
    bb = ta.volatility.BollingerBands(closes, window=20, window_dev=2)

    rsi_val = rsi.iloc[-1]
    ema9_val = ema9.iloc[-1]
    ema21_val = ema21.iloc[-1]
    macd_line = macd.macd().iloc[-1]
    macd_signal = macd.macd_signal().iloc[-1]
    bb_lower = bb.bollinger_lband().iloc[-1]
    bb_upper = bb.bollinger_hband().iloc[-1]
    last_price = closes.iloc[-1]
    prev_price = closes.iloc[-2] if len(closes) > 1 else last_price

    volumes = df.get("volume", df.get("vol", None))
    vol_current = float(volumes.iloc[-1]) if volumes is not None else 0
    vol_avg = float(volumes.tail(20).mean()) if volumes is not None else 1
    vol_ratio = round(vol_current / vol_avg, 2) if vol_avg > 0 else 1

    closes_arr = closes.values
    streak = 0
    direction = None
    for i in range(min(5, len(closes_arr) - 1)):
        if closes_arr[-(i + 1)] > closes_arr[-(i + 2)]:
            if direction is None or direction == 1:
                direction = 1
                streak += 1
            else:
                break
        elif closes_arr[-(i + 1)] < closes_arr[-(i + 2)]:
            if direction is None or direction == -1:
                direction = -1
                streak -= 1
            else:
                break
        else:
            break

    result = {
        "rsi": round(rsi_val, 2) if pd.notna(rsi_val) else None,
        "ema9": round(ema9_val, 2) if pd.notna(ema9_val) else None,
        "ema21": round(ema21_val, 2) if pd.notna(ema21_val) else None,
        "macd_line": round(macd_line, 8) if pd.notna(macd_line) else None,
        "macd_signal": round(macd_signal, 8) if pd.notna(macd_signal) else None,
        "bb_lower": round(bb_lower, 2) if pd.notna(bb_lower) else None,
        "bb_upper": round(bb_upper, 2) if pd.notna(bb_upper) else None,
        "last_price": round(last_price, 2),
        "price_change_pct": round((last_price - prev_price) / prev_price * 100, 2) if prev_price else 0,
        "volatility": round(closes.pct_change().std() * 100, 2),
        "volume_ratio": vol_ratio,
        "momentum_streak": streak,
        "momentum_dir": "up" if streak > 0 else "down" if streak < 0 else "flat",
    }

    signal = _decide_raw_signal(result, last_price)
    result["raw_signal"] = signal["decision"]
    result["signal_reason"] = signal["reason"]
    return result

def _decide_raw_signal(ind: dict, last_price: float) -> dict:
    reasons = []

    if ind["rsi"] is not None:
        if ind["rsi"] < 30:
            reasons.append("oversold_rsi")
        elif ind["rsi"] > 70:
            reasons.append("overbought_rsi")

    if ind["ema9"] is not None and ind["ema21"] is not None:
        if ind["ema9"] > ind["ema21"]:
            reasons.append("ema_bullish")
        else:
            reasons.append("ema_bearish")

    if ind["macd_line"] is not None and ind["macd_signal"] is not None:
        if ind["macd_line"] > ind["macd_signal"]:
            reasons.append("macd_bullish")
        else:
            reasons.append("macd_bearish")

    if ind["bb_lower"] is not None and last_price <= ind["bb_lower"]:
        reasons.append("near_bb_lower")
    if ind["bb_upper"] is not None and last_price >= ind["bb_upper"]:
        reasons.append("near_bb_upper")

    buy_signals = [r for r in reasons if "bullish" in r or "oversold" in r or "bb_lower" in r]
    sell_signals = [r for r in reasons if "bearish" in r or "overbought" in r or "bb_upper" in r]

    if len(buy_signals) >= 2:
        return {"decision": "BUY", "reason": "; ".join(buy_signals)}
    if len(sell_signals) >= 2:
        return {"decision": "SELL", "reason": "; ".join(sell_signals)}

    return {"decision": "HOLD", "reason": "no_clear_signal"}

def compute_batch_signals(ohlcv_map: dict[str, list[dict]]) -> dict[str, dict]:
    result = {}
    for pair, ohlcv in ohlcv_map.items():
        try:
            result[pair] = compute_signals(ohlcv)
        except Exception as e:
            result[pair] = {"raw_signal": "HOLD", "reason": f"error: {e}"}
    return result
