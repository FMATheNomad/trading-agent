# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import time
import config


def decide(all_signals, ticker_map, live_tickers, positions, actual_idr_balance,
           total_equity, regime_info, ohlcv_map_1h, coin_blacklist, pair_meta,
           book_pressure_map=None):
    ranked = _score_all_pairs(all_signals, ticker_map, live_tickers, book_pressure_map)
    trades = []
    held_pairs = {p["pair"] for p in positions}

    regime = regime_info.get("regime", "SIDEWAYS")
    is_bear = regime in ("BEAR",)

    for pos in positions:
        pair = pos["pair"]
        r = next((x for x in ranked if x["pair"] == pair), None)
        if not r:
            continue
        entry = pos.get("entry_price", 0) or 1
        price = r["price"]
        pnl = (price - entry) / entry * 100
        hold = time.time() - pos.get("entry_time", time.time())
        atr = r["atr"] or 1.0
        sell_reason = None

        cut_thresh = -max(atr * config.ATR_CUT_MULT, 4)
        if is_bear:
            cut_thresh = -max(atr * config.ATR_CUT_MULT, 3)
        if pnl < cut_thresh:
            sell_reason = f"Cut {pnl:.1f}% (ATR×{config.ATR_CUT_MULT})"

        elif r["signal"] == "SELL" and pnl < 0:
            sell_reason = f"Signal SELL"

        elif r["rank"] > len(ranked) * 0.6 and pnl < -max(atr * 0.5, 1):
            sell_reason = f"Rank {r['rank']}/{len(ranked)} turun"

        elif pnl >= atr * config.ATR_PROFIT_SELL_MULT and config.ATR_PROFIT_SELL_MULT < 20:
            sell_reason = f"Profit {pnl:.1f}% (ATR×{config.ATR_PROFIT_SELL_MULT})"

        elif hold > 14400 and pnl > 0:
            sell_reason = f"Time TP {pnl:.1f}% ({int(hold/60)}m)"

        if sell_reason:
            trades.append({"pair": pair, "action": "SELL", "allocation_pct": 100, "reason": sell_reason})

    selling = {t["pair"] for t in trades if t["action"] == "SELL"}
    remaining = len(held_pairs - selling)
    max_pos = config.max_positions_for_equity(total_equity)
    slots = max_pos - remaining

    if slots > 0 and actual_idr_balance >= config.MIN_ORDER_IDR * 3:
        min_score = 3 if is_bear else 6
        candidates = [
            r for r in ranked
            if r["pair"] not in held_pairs
            and r["pair"] not in config.STABLECOINS
            and r["pair"] not in config.SKIP_COINS
            and r["pair"] not in coin_blacklist
            and r["signal"] == "BUY"
            and r["tf_aligned"]
            and r["score"] >= min_score
            and r["vol_idr"] >= 500_000_000
            and r["price"] >= 50
            and (r["atr"] or 0) <= 25.0
        ]
        slots = min(slots, 2)
        n_bins = max(1, int(actual_idr_balance / 50000))
        n_bins = min(n_bins, slots, 3)
        per_bin = max(config.MIN_ORDER_IDR, int(actual_idr_balance * 0.35 / max(n_bins, 1)))
        for c in candidates[:n_bins]:
            alloc = int(per_bin / actual_idr_balance * 100) if actual_idr_balance > 0 else 0
            alloc = min(max(alloc, 15), 35)
            trades.append({
                "pair": c["pair"], "action": "BUY", "allocation_pct": alloc,
                "reason": f"Rank {c['rank']} s{c['score']:.0f}"
            })

    decision = "REBALANCE" if trades else "HOLD"
    reason = f"Rules: {len(trades)} trade(s)" if trades else \
             f"Wait — top score {ranked[0]['score']:.0f}" if ranked else \
             "Wait — no data"

    cash_low = actual_idr_balance < 200_000
    play_pct = 90 if config.INSANE_MODE else (50 if is_bear else (65 if cash_low else 55))

    return {
        "decision": decision,
        "reasoning": reason,
        "trades": trades,
        "play_capital_pct": play_pct,
    }


def _score_all_pairs(all_signals, ticker_map, live_tickers, book_pressure_map=None):
    results = []
    for pair, sig in all_signals.items():
        total = 50.0
        raw = sig.get("raw_signal", "HOLD")
        score_val = sig.get("score", 0)

        if raw == "BUY":
            total += min(abs(score_val) * 3, 20)
        elif raw == "SELL":
            total -= 15

        if sig.get("timeframe_aligned") and raw == "BUY":
            total += 10

        vr = sig.get("volume_ratio", 1)
        if vr > 2.0:
            total += 10
        elif vr > 1.5:
            total += 5
        elif vr > 1.2:
            total += 2

        rsi = sig.get("rsi")
        if rsi is not None:
            if rsi < 35:
                total += 8
            elif rsi < 45:
                total += 4
            elif rsi > 70:
                total -= 6

        ema9 = sig.get("ema9")
        ema21 = sig.get("ema21")
        if ema9 and ema21 and ema9 > ema21:
            total += 5

        ema21_v = sig.get("ema21")
        ema50 = sig.get("ema50")
        if ema21_v and ema50 and ema21_v > ema50:
            total += 3

        atr = sig.get("atr_pct", 0)
        if 1.5 <= atr <= 6:
            total += 5
        elif atr > 10:
            total -= 5

        streak = sig.get("momentum_streak", 0)
        if streak >= 2:
            total += 4
        elif streak <= -2:
            total -= 3

        lt = live_tickers.get(pair, {})
        chg24 = lt.get("change_24h", 0)
        if chg24 > 3 and raw == "BUY":
            total += 5
        elif chg24 < -5:
            total -= 3

        book = book_pressure_map.get(pair) if book_pressure_map else None
        if book:
            if book.get("imbalance_pct", 0) > 5:
                total += 4
            elif book.get("imbalance_pct", 0) < -5:
                total -= 3

        ticker = ticker_map.get(pair, {})
        price = ticker.get("sell", 0) or lt.get("last", 0)
        vol_idr = float(ticker.get("vol_idr", 0))

        results.append({
            "pair": pair,
            "score": round(total, 1),
            "signal": raw,
            "rsi": rsi,
            "atr": atr,
            "volume_ratio": vr,
            "tf_aligned": sig.get("timeframe_aligned", False),
            "price": price,
            "vol_idr": vol_idr,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
    return results

