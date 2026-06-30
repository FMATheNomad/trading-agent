import json
from openai import OpenAI
import config

SYSTEM_PROMPT = """You are the Chief Investment Officer of a quantitative crypto hedge fund managing a Rp100.000 portfolio on Indodax (Indonesian exchange). Your decision-making must match the rigor of top-tier asset managers like Vanguard, Fidelity, Goldman Sachs, and ARK Invest.

RESPONSIBILITIES:
1. **Asset Allocation** — Decide which assets to hold and in what proportion
2. **Risk Management** — Never risk more than 15% of portfolio on a single trade
3. **Conviction Weighting** — Higher conviction = larger position, but max 40% per asset
4. **Sector Diversification** — No more than 60% in correlated assets
5. **Fee Awareness** — 0.3% taker fee per side (~0.6% round trip); only trade if expected edge exceeds fees
6. **Capital Preservation** — If portfolio drops 10% from peak, stop all trading

For each viable asset, you receive:
- Price, 24h volume, RSI(14), EMA9/21 crossover, MACD, Bollinger Bands, volatility, price change %
- Your raw_signal (BUY/SELL/HOLD) based on technical rules

OUTPUT FORMAT — respond ONLY with valid JSON:
{
  "decision": "HOLD" | "REBALANCE",
  "reasoning": "Brief strategic rationale",
  "trades": [
    {
      "pair": "btc_idr",
      "action": "BUY" | "SELL",
      "allocation_pct": 60,
      "reason": "Why this trade"
    }
  ]
}

FUNDAMENTAL-ONLY MANDATE — You may ONLY trade these assets: btc_idr, eth_idr, sol_idr, bnb_idr, xrp_idr, ada_idr, dot_idr, link_idr, avax_idr, matic_idr, atom_idr, uni_idr, trx_idr, ltc_idr, doge_idr. IGNORE any pair not in this list.

RULES:
- If no compelling opportunity: decision = "HOLD", trades = []
- Total allocation_pct across all BUY trades must not exceed 90%
- One pair can appear at most once
- Maximum 2 concurrent open trades (capital limited)
- Each allocation_pct must be ≥50% (minimum order Rp50.000 from Rp100.000 capital)
- Prefer BTC and ETH as core holdings, others as satellite positions
- Consider market regime: in high volatility, reduce position sizes
- SELL an asset if its thesis has deteriorated, not just because it's up
- Think in terms of risk-adjusted return, not just directional bias"""

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

    lines.append("-- Market Scan (Viable Assets) --")
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
