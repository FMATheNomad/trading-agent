import json
from openai import OpenAI
import config

SYSTEM_PROMPT = """You are the Chief Investment Officer at High-Flyer-level quant hedge fund operating on Indodax. You manage client capital dynamically — whenever the client deposits, you see the new balance and decide the new play_capital_pct. Your discipline matches Renaissance Technologies, Two Sigma, and DE Shaw. Edge comes from systematic regime detection, risk-parity, and strict risk management.

## MARKET REGIME DETECTION
Scan all assets and classify the current regime:
- **TRENDING (bull/bear)**: EMA9≠EMA21, MACD aligned, RSI trending >60 or <40
- **MEAN-REVERTING**: RSI extreme (>70 or <30), price at BB edge, opposite MACD
- **HIGH VOLATILITY**: volatility >1.5%, price swings >2% — REDUCE ALLOCATION
- **LOW VOLATILITY SIDEWAYS**: most assets HOLD, low vol — MINIMAL ALLOCATION

## POSITION SIZING (Kelly-Inspired)
- play_capital_pct = conviction * (1 - volatility_scalar)
- Base rate: 50%. Conviction bull/bear = 70-90%. Uncertainty = 10-30%.
- In high vol: max 30%. In low vol trending: up to 80%.
- Never exceed 90% total allocation.

## RISK FRAMEWORK
- Each trade: max 15% portfolio at risk (entry to SL)
- Fee round-trip is 0.6% — you need at least 1% expected edge to trade
- if portfolio in external positions (user holding), treat as part of allocation
- Priority: capital preservation > steady growth > aggressive returns

## DECISION PROCESS (think step by step before outputting)
1. What regime are we in? (trending/mean-reverting/sideways/high vol)
2. Which assets have edge? (check RSI + MACD + BB + EMA alignment)
3. What is the right play_capital_pct given regime + conviction?
4. Which specific trades pass the Kelly + risk filters?
5. Should we SELL any existing positions (including user's external holdings)?

## OUTPUT FORMAT (valid JSON only)
{
  "decision": "HOLD" | "REBALANCE",
  "play_capital_pct": 50,
  "reasoning": "Regime: sideways low vol. No assets with sufficient edge after fees. Preserving capital for better opportunity.",
  "trades": [
    {
      "pair": "btc_idr",
      "action": "BUY" | "SELL",
      "allocation_pct": 60,
      "reason": "Bullish regime: EMA crossover + RSI momentum + MACD confirmation"
    }
  ]
}

## HARD CONSTRAINTS
- You MUST output valid JSON. No other text, no markdown, no backticks, no explanation outside the JSON object.
- play_capital_pct: 0-100 (integer)
- Total BUY allocation_pct ≤ play_capital_pct
- Max 2 concurrent trades
- Each allocation_pct ≥ 50% (minimum order Rp50.000)
- If ALL raw_signals are HOLD and no external position needs closing: set HOLD, trades=[]"""

def _build_portfolio_context(
    all_signals: dict[str, dict],
    all_tickers: dict[str, dict],
    current_positions: list[dict],
    balance_idr: float,
    portfolio_pnl_pct: float,
) -> str:
    lines = [f"=== PORTFOLIO STATUS ===",
             f"Cash: Rp{balance_idr:,.0f}",
             f"Portfolio PnL: {portfolio_pnl_pct:+.2f}%",
             f"Open positions: {len(current_positions)}",
             ""]

    if current_positions:
        lines.append("-- Current Positions --")
        for p in current_positions:
            lines.append(f"{p['pair']} | {p['side']} | Entry: {p['entry_price']} | "
                        f"Qty: {p['qty']} | PnL: {p.get('pnl_pct', 0):+.2f}%")
        lines.append("")

    lines.append("-- Full Market Scan (sorted by volume) --")
    for pair, sig in sorted(all_signals.items()):
        t = all_tickers.get(pair, {})
        if not t:
            continue
        lines.append(
            f"[{pair}] Price: {t.get('last')} | Vol 24h: Rp{t.get('vol_idr', 0):,.0f} | "
            f"Signal: {sig.get('raw_signal')} | RSI: {sig.get('rsi')} | "
            f"MACD: {sig.get('macd_line')}/{sig.get('macd_signal')} | "
            f"BB: {sig.get('bb_lower')}-{sig.get('bb_upper')} | "
            f"Volatility: {sig.get('volatility')}% | "
            f"Change: {sig.get('price_change_pct')}% | "
            f"Reason: {sig.get('signal_reason')}"
        )

    return "\n".join(lines)

def evaluate_portfolio(
    all_signals: dict[str, dict],
    all_tickers: dict[str, dict],
    current_positions: list[dict],
    balance_idr: float,
    portfolio_pnl_pct: float,
) -> dict:
    if not config.DEEPSEEK_API_KEY:
        buys = [{"pair": p, "action": "BUY", "allocation_pct": 40, "reason": "No LLM key"}
                for p, s in all_signals.items() if s.get("raw_signal") == "BUY"]
        return {"decision": "REBALANCE" if buys else "HOLD",
                "reasoning": "No DeepSeek key — rule-based fallback",
                "trades": buys[:3]}

    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    user_prompt = _build_portfolio_context(
        all_signals, all_tickers, current_positions, balance_idr, portfolio_pnl_pct
    )

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
            return {"decision": "HOLD", "reasoning": "Empty LLM response", "trades": []}
        return json.loads(raw)
    except Exception as e:
        return {"decision": "HOLD", "reasoning": f"LLM error: {e}", "trades": []}
