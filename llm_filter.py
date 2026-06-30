import json
from openai import OpenAI
import config

BASE_PROMPT = """You are CIO of FMA ALPHA QUANT LABS. Your ONLY mission: grow Rp200.000 → Rp500.000 in days.

This is a small account. Every trade must count. You eat what you kill.

## REGIME
- **BULL** (+25% buys or positive score): play 60-95%. Trend-follow, size up aggressively.
- **SIDEWAYS**: play 50-75%. Mean-reversion, quick scalps.
- **BEAR** (+50% sells, score <-1): play 20-40%. Selective shorts only if already holding.
- **HIGH_VOL** (vol >3.5%): opportunity. Play 60-90%, wider stops.

## SIGNALS
1. ⚡Stat-Arb (z>2 or z<-2) on BTC/ETH, SOL/ADA, BNB/XRP
   - CANNOT short sell. SELL only for coins already owned.
2. 🔥Hot Now (vol spike + momentum + not SELL)
3. TF_aligned (1h+4h same direction) = high conviction
4. Gainer/Loser 24h with momentum confirmation
- BUY at score ≥+2, SELL at ≤-3. Score +2 with vol spike = TRADE NOW.

## OUTPUT (json)
{"decision":"HOLD|REBALANCE","play_capital_pct":0-100,"reasoning":"...","trades":[{"pair":"","action":"BUY|SELL","allocation_pct":N,"reason":""}]}
- Max 4 total positions (bot + user external combined)
- play_capital_pct must be ≥70% when cash <Rp200k
- allocation_pct per BUY must be ≥70% (maximize every trade)
- Total allocation ≤ play_capital_pct"""

ALPHA_PROMPT = """

## ALPHA MODE ACTIVE — target: 200k → 500k 🔥
Target: 25% profit per trade, 8% stop loss. Risk ~1:3 ratio.
- BUY score ≥+2 is tradeable — lower bar, higher frequency
- Hot Now + any BUY signal = execute immediately
- TF alignment preferred but NOT required
- Play capital 70-95% on high conviction setups
- CRITICAL: Account Rp200k. Butuh ~4 trade @25% untuk 2.5x lipat. Jangan HOLD terus — cari edge SETIAP cycle. Tapi jangan maksa kalo bener-bener gak ada setup (volume sepi, sideways tipis).
- Prioritaskan coin dengan vol spike + momentum kuat + 24h gainer
"""

SYSTEM_PROMPT = BASE_PROMPT + (ALPHA_PROMPT if config.ALPHA_MODE else "")

_strategy_map = {
    "BULL": "AGGRESSIVE (trend-follow, size up)",
    "SIDEWAYS": "SCALP (mean-reversion, quick exits)",
    "SIDEWAYS_LOW_VOL": "SCALP (tight range trades)",
    "BEAR": "SELECTIVE (shorts only, high bar)",
    "HIGH_VOL": "NORMAL (vol is opportunity, wider stops)",
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
    pair_signals: list[dict] | None = None,
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

    if pair_signals:
        lines.append("-- Stat-Arb Pairs (Renaissance-style) --")
        for ps in pair_signals:
            icon = "⚡" if "SHORT" in ps.get("signal", "") or "LONG" in ps.get("signal", "") else " "
            lines.append(f"  {icon}{ps['pair']}: z={ps['z_score']} ratio={ps['ratio']} "
                        f"mean={ps.get('mean_ratio', '?')} → {ps['signal']} {ps.get('reason', '')}")
        lines.append("")

    if pair_suggestions:
        lines.append("-- Cross Pairs Monitor --")
        for p in pair_suggestions:
            lines.append(f"  {p['pair']} = {p['ratio']} (A: {p['a_price']}, B: {p['b_price']})")
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
        lines.append("-- Portfolio Breakdown --")
        for p in current_positions:
            is_ext = " [USER POSITION]" if p.get("real") else ""
            lines.append(f"  {p['pair']} {p['side']} | Entry:{p['entry_price']} | "
                        f"Qty:{p['qty']} | PnL:{p.get('pnl_pct', 0):+.2f}% | "
                        f"Value:Rp{p.get('current_value', 0):,.0f}{is_ext}")
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
    pair_signals: list[dict] | None = None,
) -> dict:
    if not config.DEEPSEEK_API_KEY:
        buys = [{"pair": p, "action": "BUY", "allocation_pct": 80, "reason": "No LLM key"}
                for p, s in all_signals.items() if s.get("raw_signal") == "BUY"]
        return {"decision": "REBALANCE" if buys else "HOLD",
                "reasoning": "No DeepSeek key — rule-based fallback",
                "trades": buys[:3]}

    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    user_prompt = _build_portfolio_context(
        all_signals, all_tickers, current_positions, balance_idr, portfolio_pnl_pct,
        regime_info, pair_suggestions, regime_history, orderbooks, live_tickers, new_coins, pair_signals,
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
