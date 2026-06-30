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

## REGIME-BASED STRATEGY
Your behavior changes based on regime:
- **STRONG_BULL**: aggressive (play_capital 60-90%), chase breakouts, hold longer
- **BULL**: moderately aggressive (40-60%), trend-follow
- **SIDEWAYS/SIDEWAYS_LOW_VOL**: pairs/mean-reversion, quick scalps (20-40%)
- **BEAR**: defensive (10-30%), only high-conviction shorts, cut fast
- **STRONG_BEAR/HIGH_VOL**: capital preservation (0-10%), mostly HOLD, wait
- Regime consistency matters: if regime changed in last 3 cycles, reduce size

## MULTI-TIMEFRAME ANALYSIS
Each asset shows TWO timeframes:
- **1h** (short-term): entry timing, momentum
- **4h** (medium-term): macro trend, conviction
- **TF_aligned**: when 1h and 4h agree on same direction = HIGH CONVICTION setup
- **Score**: multi-factor score (RSI + EMA + MACD + BB + volume + momentum)
  - BUY ≥ +4, SELL ≤ -3, HOLD in between
- **Range14**: 14-period range % — narrow range = potential breakout

## TRENDING / MOMENTUM DETECTION
Assets ranked by combined momentum+24h score. You also get:
- **🔥 Hot Now** = volume spike >1.5x + positive 1h momentum + not SELL signal — these are coins ACTIVE right now
- **Top Gainer 24h** = highest 24h % change — potentially overbought
- **Top Loser 24h** = lowest 24h % change — potentially oversold
- **24h%** = big moves in last day (pump/dump detection)
- **1h%** = recent momentum (short-term entry timing)
- **Conviction:HIGH** = 1h + 4h aligned = strongest signal
- **🚀 Volume spike** = vol > 2x average
- Highest conviction: **🔥 Hot Now + HIGH conviction + BUY signal**

## DECISION PROCESS
1. Market regime? (check 4h trends across assets)
2. Which assets have HIGH conviction? (timeframe aligned + score ≥4)
3. Any breakout candidates? (narrow range + volume spike)
4. What play_capital_pct based on regime strength?
5. Which specific trades pass Kelly + risk filters?
6. SELL any external positions if thesis broken?

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

_strategy_map = {
    "STRONG_BULL": "TREND_FOLLOW (aggressive, hold winners)",
    "BULL": "TREND_FOLLOW (moderate)",
    "SIDEWAYS": "MEAN_REVERSION (fade extremes, quick exits)",
    "SIDEWAYS_LOW_VOL": "MEAN_REVERSION (tight stops, small targets)",
    "BEAR": "DEFENSIVE (short only, high conviction)",
    "STRONG_BEAR": "CAPITAL_PRESERVATION (mostly cash)",
    "HIGH_VOL": "REDUCED_SIZE (wide stops, low leverage)",
}

def _build_portfolio_context(
    all_signals: dict[str, dict],
    all_tickers: dict[str, dict],
    current_positions: list[dict],
    balance_idr: float,
    portfolio_pnl_pct: float,
    regime_info: dict | None = None,
    pair_suggestions: list[dict] | None = None,
    regime_history: list[str] | None = None,
    orderbooks: dict[str, dict] | None = None,
    live_tickers: dict[str, dict] | None = None,
    new_coins: set[str] | None = None,
) -> str:
    lines = [f"=== PORTFOLIO STATUS ===",
             f"Cash: Rp{balance_idr:,.0f}",
             f"Portfolio PnL: {portfolio_pnl_pct:+.2f}%",
             f"Open positions: {len(current_positions)}",
             ""]

    if regime_info:
        lines.append(f"-- Market Regime: {regime_info.get('regime', 'N/A')} --")
        lines.append(f"Buy ratio: {regime_info.get('buy_ratio', 0)} | Sell ratio: {regime_info.get('sell_ratio', 0)}")
        lines.append(f"Avg score: {regime_info.get('avg_score', 0)} | High conviction: {regime_info.get('high_conviction_count', 0)}")
        lines.append(f"Avg volatility: {regime_info.get('avg_volatility', 0)}%")
        if regime_history:
            lines.append(f"Regime history (last {len(regime_history)}): {' → '.join(regime_history[-8:])}")
        lines.append("")

    if pair_suggestions:
        lines.append("-- Pairs Monitor --")
        for p in pair_suggestions:
            lines.append(f"{p['pair']} = {p['ratio']} (A: {p['a_price']}, B: {p['b_price']})")
        lines.append("")

    if regime_info:
        strat = _strategy_map.get(regime_info.get("regime", ""), "NEUTRAL")
        lines.append(f"-- Active Strategy: {strat} --")
        lines.append("")

    if orderbooks:
        lines.append("-- Order Book Pressure (top 5 pairs) --")
        for pair, ob in list(orderbooks.items())[:5]:
            lines.append(f"{pair}: {ob.get('pressure', 'N/A')} ({ob.get('imbalance_pct', 0):+.1f}%) | "
                        f"BidVol:{ob.get('bid_vol', 0):.2f} AskVol:{ob.get('ask_vol', 0):.2f}")
        lines.append("")

    trending = []
    for pair, sig in all_signals.items():
        vr = sig.get("volume_ratio", 1)
        chg = sig.get("price_change_pct", 0) or 0
        if vr > 1.5 and chg > 1 and sig.get("raw_signal") != "SELL":
            trending.append((pair, vr, chg, sig.get("raw_signal", "")))
    trending.sort(key=lambda x: x[1] * x[2], reverse=True)

    if new_coins:
        lines.append(f"-- 🆕 New Listings ({len(new_coins)}) --")
        for pid in sorted(new_coins)[:5]:
            lc = live_tickers.get(pid, {}).get("change_24h", 0) if live_tickers else 0
            sig = all_signals.get(pid, {}).get("raw_signal", "?")
            lines.append(f"  {pid}: 24h:{lc:+.2f}% | Sig:{sig}")
        lines.append("")

    if trending:
        lines.append("-- 🔥 Hot Now (volume spike + momentum) --")
        for pair, vr, chg, sig in trending[:5]:
            lines.append(f"  {pair}: Vol(x{vr:.1f}) | 1h:{chg:+.2f}% | Sig:{sig}")
        lines.append("")

    if live_tickers:
        gainers = sorted(live_tickers.items(), key=lambda x: x[1].get("change_24h", 0), reverse=True)[:7]
        losers = sorted(live_tickers.items(), key=lambda x: x[1].get("change_24h", 0))[:5]
        lines.append("-- Top 5 Gainer 24h --")
        for p, t in gainers:
            sig = all_signals.get(p, {})
            vr = sig.get("volume_ratio", 1)
            spike = "🚀" if vr > 2 else " "
            lines.append(f"  {spike}{p}: {t.get('change_24h', 0):+.2f}% | Vol:1h({vr:.1f}x) | Sig:{sig.get('raw_signal','?')}")
        lines.append("")
        lines.append("-- Top 5 Loser 24h --")
        for p, t in losers:
            sig = all_signals.get(p, {})
            vr = sig.get("volume_ratio", 1)
            lines.append(f"  {p}: {t.get('change_24h', 0):+.2f}% | Vol:({vr:.1f}x) | Sig:{sig.get('raw_signal','?')}")
        lines.append("")

    if current_positions:
        lines.append("-- Current Positions --")
        for p in current_positions:
            lines.append(f"{p['pair']} | {p['side']} | Entry: {p['entry_price']} | "
                        f"Qty: {p['qty']} | PnL: {p.get('pnl_pct', 0):+.2f}%")
        lines.append("")

    def _chg24(t: dict, live: dict | None) -> float:
        if live and live.get("change_24h") is not None:
            return live["change_24h"]
        hi = t.get("high_24h", 0) or t.get("high", 0)
        lo = t.get("low_24h", 0) or t.get("low", 0)
        last = t.get("last", 0)
        if hi and last:
            return round((last / ((hi + lo) / 2) - 1) * 100, 2)
        return 0

    scored = []
    for pair, sig in all_signals.items():
        t = all_tickers.get(pair, {})
        if not t:
            continue
        lt = live_tickers.get(pair) if live_tickers else None
        chg24 = _chg24(t, lt)
        combined_score = abs(sig.get("score", 0)) + abs(chg24 / 5)
        scored.append((combined_score, pair, sig, t, chg24))

    scored.sort(key=lambda x: x[0], reverse=True)

    lines.append(f"-- Market Scan ({len(all_signals)} pairs, ranked by momentum+24h) --")
    for rank, (_, pair, sig, t, chg24) in enumerate(scored, 1):
        vol_spike = "🚀" if sig.get("volume_ratio", 0) > 2 else " "
        lines.append(
            f"#{rank} {vol_spike}[{pair}] Price: {t.get('last')} | "
            f"24h:{chg24:+.2f}% | "
            f"1h:{sig.get('price_change_pct', 0):+.2f}% | "
            f"Vol:Rp{t.get('vol_idr', 0):,.0f} (x{sig.get('volume_ratio', 1)}avg) | "
            f"1h:{sig.get('raw_signal')}({sig.get('score', 0)}) | "
            f"4h:{sig.get('4h_signal', 'N/A')}({sig.get('4h_score', 0)}) | "
            f"TF:{'Y' if sig.get('timeframe_aligned') else 'N'} | "
            f"CV:{sig.get('conviction', 'LOW')} | "
            f"RSI:{sig.get('rsi')} | MACD:{sig.get('macd_line')}/{sig.get('macd_signal')} | "
            f"BB:{sig.get('bb_lower')}-{sig.get('bb_upper')} | "
            f"Vol:{sig.get('volatility')}% | R14:{sig.get('range_14_pct')}% | "
            f"M:{sig.get('momentum_streak', 0)}{sig.get('momentum_dir', '')} | "
            f"R:{sig.get('signal_reason')}"
        )

    return "\n".join(lines)

def evaluate_portfolio(
    all_signals: dict[str, dict],
    all_tickers: dict[str, dict],
    current_positions: list[dict],
    balance_idr: float,
    portfolio_pnl_pct: float,
    regime_info: dict | None = None,
    pair_suggestions: list[dict] | None = None,
    regime_history: list[str] | None = None,
    orderbooks: dict[str, dict] | None = None,
    live_tickers: dict[str, dict] | None = None,
    new_coins: set[str] | None = None,
) -> dict:
    if not config.DEEPSEEK_API_KEY:
        buys = [{"pair": p, "action": "BUY", "allocation_pct": 40, "reason": "No LLM key"}
                for p, s in all_signals.items() if s.get("raw_signal") == "BUY"]
        return {"decision": "REBALANCE" if buys else "HOLD",
                "reasoning": "No DeepSeek key — rule-based fallback",
                "trades": buys[:3]}

    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    user_prompt = _build_portfolio_context(
        all_signals, all_tickers, current_positions, balance_idr, portfolio_pnl_pct,
        regime_info, pair_suggestions, regime_history, orderbooks, live_tickers, new_coins,
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
        err_msg = f"LLM error: {e}"
        print(f"DeepSeek API error: {e}", flush=True)
        return {"decision": "HOLD", "reasoning": err_msg, "trades": [], "deepseek_error": True}
