import pandas as pd
import ta

def compute_signals(ohlcv: list[dict]) -> dict:
    if len(ohlcv) < 30:
        return {"raw_signal": "HOLD", "reason": "insufficient_data"}

    df = pd.DataFrame(ohlcv)
    df.columns = [c.lower() for c in df.columns]
    closes = df["close"].astype(float)

    rsi = ta.momentum.RSIIndicator(closes, window=14).rsi().iloc[-1]
    ema9 = ta.trend.EMAIndicator(closes, window=9).ema_indicator().iloc[-1]
    ema21 = ta.trend.EMAIndicator(closes, window=21).ema_indicator().iloc[-1]
    macd = ta.trend.MACD(closes)
    macd_line = macd.macd().iloc[-1]
    macd_signal = macd.macd_signal().iloc[-1]
    bb = ta.volatility.BollingerBands(closes, window=20, window_dev=2)

    result = {
        "rsi": round(rsi, 2) if pd.notna(rsi) else None,
        "ema9": round(ema9, 2) if pd.notna(ema9) else None,
        "ema21": round(ema21, 2) if pd.notna(ema21) else None,
        "macd_line": round(macd_line, 8) if pd.notna(macd_line) else None,
        "macd_signal": round(macd_signal, 8) if pd.notna(macd_signal) else None,
        "bb_lower": round(bb.bollinger_lband().iloc[-1], 2) if pd.notna(bb.bollinger_lband().iloc[-1]) else None,
        "bb_upper": round(bb.bollinger_hband().iloc[-1], 2) if pd.notna(bb.bollinger_hband().iloc[-1]) else None,
    }

    signal = _decide_raw_signal(result, closes.iloc[-1])
    result["raw_signal"] = signal["decision"]
    result["signal_reason"] = signal["reason"]
    result["last_price"] = round(closes.iloc[-1], 2)
    return result

def _decide_raw_signal(ind: dict, last_price: float) -> dict:
    reasons = []

    if ind["rsi"] is not None and ind["rsi"] < 30:
        reasons.append("oversold_rsi")
    elif ind["rsi"] is not None and ind["rsi"] > 70:
        reasons.append("overbought_rsi")

    if ind["ema9"] is not None and ind["ema21"] is not None:
        if ind["ema9"] > ind["ema21"]:
            reasons.append("ema_bullish_cross")
        else:
            reasons.append("ema_bearish_cross")

    if ind["macd_line"] is not None and ind["macd_signal"] is not None:
        if ind["macd_line"] > ind["macd_signal"]:
            reasons.append("macd_bullish")
        else:
            reasons.append("macd_bearish")

    if ind["bb_lower"] is not None and last_price <= ind["bb_lower"]:
        reasons.append("price_at_bb_lower")
    if ind["bb_upper"] is not None and last_price >= ind["bb_upper"]:
        reasons.append("price_at_bb_upper")

    buy_signals = [r for r in reasons if "bullish" in r or "oversold" in r or "bb_lower" in r]
    sell_signals = [r for r in reasons if "bearish" in r or "overbought" in r or "bb_upper" in r]

    if len(buy_signals) >= 2:
        return {"decision": "BUY", "reason": "; ".join(buy_signals)}
    if len(sell_signals) >= 2:
        return {"decision": "SELL", "reason": "; ".join(sell_signals)}

    return {"decision": "HOLD", "reason": "no_clear_signal"}
