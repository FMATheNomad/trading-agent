import json
from openai import OpenAI
import config

SYSTEM_PROMPT = """You are a cautious trading advisor for a small retail account (Rp100.000).
You receive technical indicators and account state, then decide whether to trade.
Rules:
- Only CONFIRM if the signal is strong and risk is acceptable.
- REJECT if unsure or too risky.
- ADJUST if the position size needs reduction.
- Always consider fees (0.3% taker each side, ~0.6% round-trip).
- TP is +3%, SL is -3%.
- Never risk more than 15% of the account on a single trade.
- Respond ONLY in valid JSON with keys: decision (CONFIRM|REJECT|ADJUST), adjusted_size_pct (number or null), reasoning (string)."""

def build_user_context(
    ticker: dict,
    signals: dict,
    balance_idr: float,
    balance_coin: float,
    has_open_position: bool,
    position_entry_price: float | None,
    trade_history: list,
) -> str:
    ctx = f"""
Current Price (last): {ticker.get('last')}
Bid/Ask: {ticker.get('buy')} / {ticker.get('sell')}
Indicators:
  RSI(14): {signals.get('rsi')}
  EMA9: {signals.get('ema9')}
  EMA21: {signals.get('ema21')}
  MACD line / signal: {signals.get('macd_line')} / {signals.get('macd_signal')}
  Bollinger Lower: {signals.get('bb_lower')} / Upper: {signals.get('bb_upper')}
Raw Signal: {signals.get('raw_signal')} — {signals.get('signal_reason')}

Account:
  Balance IDR: {balance_idr}
  Balance Coin: {balance_coin}
  Has Open Position: {has_open_position}
  Entry Price: {position_entry_price}

Last 3 Trades:
"""
    for t in trade_history[-3:]:
        ctx += f"  {t}\n"
    return ctx

def evaluate(signals: dict, ticker: dict, balance_idr: float, balance_coin: float,
             has_open_position: bool, position_entry_price: float | None,
             trade_history: list) -> dict:
    if not config.DEEPSEEK_API_KEY:
        return {"decision": "CONFIRM" if signals.get("raw_signal") != "HOLD" else "REJECT",
                "adjusted_size_pct": None, "reasoning": "No DeepSeek key — bypassing LLM filter"}

    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    user_prompt = build_user_context(ticker, signals, balance_idr, balance_coin,
                                     has_open_position, position_entry_price, trade_history)

    kwargs = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    if config.DEEPSEEK_THINKING_MODE:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    try:
        resp = client.chat.completions.create(**kwargs)
        raw = resp.choices[0].message.content
        if not raw:
            return {"decision": "REJECT", "adjusted_size_pct": None,
                    "reasoning": "Empty response from LLM"}
        return json.loads(raw)
    except Exception as e:
        return {"decision": "REJECT", "adjusted_size_pct": None,
                "reasoning": f"LLM error: {e}"}
