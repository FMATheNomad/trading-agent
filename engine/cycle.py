# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""engine/cycle.py — satu siklus keputusan portfolio (regime → sinyal → order).

Dipotong dari main.py (#8 tahap 3). Fungsi ini dulu 1.290 baris di dalam file
entrypoint, jadi satu-satunya cara mengujinya adalah menjalankan bot sungguhan.
Sekarang `main.py` cuma mengatur proses (WS, poller, graceful shutdown) dan
menyerahkan seluruh keputusan trading ke modul ini.

Kontrak: `portfolio_cycle` adalah penulis tunggal state keputusan dan wajib
dipanggil di dalam `S._state_lock` — ia tidak boleh di-`await` lagi di tengah
baca-tulis `S.positions`/`registry`. Semua jalur mutasi (tick WS, fill WS, DCA,
momentum BUY, cycle) berebut lock yang sama.
"""

import asyncio
import datetime
import math
import time

import httpx

import config
import persist
import pairs
import rules
from data_layer import fetch_viable_pairs, fetch_ticker, fetch_ohlcv_both, fetch_orderbook
from deadman import refresh_deadman
from db import log_trade, log_decision, get_trade_count_today, get_trades_by_period
from executor import (place_order, get_balance, get_order, cancel_order,
                      get_open_orders, quantize_price, refresh_pair_rules)
from indicators import compute_batch_signals
from market_ws import LIVE_TICKERS
from notifier import send_message
from orders import registry, ENTRY, SELL
from engine import state as S
from engine.position_sm import (add_position, pnl_pct, _sm_cancel, _sm_place_sell,
                                _sm_place_tp, _sm_place_sl, _sm_init, _sm_cleanup)


def classify_regime(all_signals: dict, ohlcv_map_1h: dict | None = None) -> dict:
    signals = [s.get("raw_signal") for s in all_signals.values() if s.get("raw_signal")]
    scores = [s.get("score", 0) for s in all_signals.values() if s.get("score") is not None]
    vols = [s.get("volatility", 0) for s in all_signals.values() if s.get("volatility") is not None]

    buys = signals.count("BUY")
    sells = signals.count("SELL")
    total = len(signals) or 1
    avg_score = sum(scores) / len(scores) if scores else 0
    avg_vol = sum(vols) / len(vols) if vols else 0
    high_conviction = sum(1 for s in all_signals.values() if s.get("conviction") == "HIGH")

    try:
        hmm_result = S.hmm_detector.predict(ohlcv_map_1h) if ohlcv_map_1h else {}
    except Exception:
        hmm_result = {}
    hmm_regime = hmm_result.get("regime", "UNKNOWN")
    hmm_conf = hmm_result.get("confidence", 0)

    if hmm_regime != "UNKNOWN" and hmm_conf > 0.6:
        regime = hmm_regime
    else:
        if buys / total >= 0.4 and avg_score > 0:
            regime = "BULL"
        elif buys / total >= 0.25 and avg_score > 0 and high_conviction >= 2:
            regime = "BULL"
        elif sells / total >= 0.5 and avg_score < -1:
            regime = "BEAR"
        elif avg_vol > config.HIGH_VOL_THRESHOLD:
            regime = "HIGH_VOL"
        elif avg_vol < 0.5 and buys / total < 0.2 and sells / total < 0.2:
            regime = "SIDEWAYS_LOW_VOL"
        else:
            regime = "SIDEWAYS"

    return {
        "regime": regime,
        "hmm_regime": hmm_regime,
        "hmm_confidence": hmm_conf,
        "buy_ratio": round(buys / total, 2),
        "sell_ratio": round(sells / total, 2),
        "avg_score": round(avg_score, 1),
        "avg_volatility": round(avg_vol, 2),
        "high_conviction_count": high_conviction,
        "total_signals": total,
    }

def correlation_risk(positions: list[dict], ticker_map: dict, threshold: float = 0.85) -> list[str]:
    if len(positions) < 2:
        return []
    high_risk = []
    prices = []
    labels = []
    for p in positions:
        pid = p["pair"]
        t = ticker_map.get(pid, {})
        price = t.get("last", p.get("entry_price", 0))
        if price:
            prices.append(price)
            labels.append(pid)
    if len(prices) < 2:
        return []
    for i in range(len(prices)):
        for j in range(i + 1, len(prices)):
            if prices[i] > 0 and prices[j] > 0:
                ratio = max(prices[i], prices[j]) / min(prices[i], prices[j])
                if ratio < 1.1:
                    high_risk.append(f"{labels[i]}+{labels[j]}")
    return high_risk

async def portfolio_cycle(client: httpx.AsyncClient):
    S.cycle_counter += 1
    _t0 = time.time()
    S.risk.daily_loss_stopped = False
    actual_idr_balance = 0
    _idr_held = 0
    _latest_full_balance = {}
    _latest_full_hold = {}

    if S._cb_active_until > time.time():
        print(f"🛑 CIRCUIT BREAKER ACTIVE — cooling down until {datetime.datetime.fromtimestamp(S._cb_active_until).strftime('%Y-%m-%d %H:%M')} WIB", flush=True)
        return
    if S._cb_active_until > 0 and S._cb_active_until <= time.time():
        S._cb_consecutive_loss_days = 0
        S._cb_active_until = 0
        S._cb_triggered_at = 0
        await send_message("✅ CIRCUIT BREAKER selesai — bot lanjut normal")
        persist.save_circuit_breaker({"consecutive_loss_days": 0, "last_loss_date": "", "triggered_at": 0, "active_until": 0})

    for po in registry.list(ENTRY):
        pid = po.pair
        try:
            oid = po.order_id
            order_info = await get_order(client, oid, pair=pid)
            if not order_info:
                registry.pop(pid, ENTRY)
                continue
            status = order_info.get("status", "").lower()
            remain = float(order_info.get("remain_rp", order_info.get("remain_idr", 0)) or 0)
            is_filled = status in ("filled",) or remain == 0
            if is_filled and po.is_maker:
                add_position(S.positions, pid, po.side.upper(), po.price, po.qty,
                             po.amount_idr, po.atr_pct, time.time(),
                             "ROTHSCHILD" if S._rothschild_active else "KONSERVATIF")
                persist.save_positions(S.positions)
                print(f"  MAKER FILLED: {pid} → position opened", flush=True)
                await send_message(f"✅ MAKER FILLED: BUY {pid}\nRp{po.amount_idr:,.0f} @ {po.price:,.0f}")
                registry.pop(pid, ENTRY)
                continue
            if status in ("cancelled", "rejected") or remain == 0:
                registry.pop(pid, ENTRY)
                continue
            if po.is_maker:
                grace = max(1, int(config.LIMIT_GRACE_CYCLE))
                if registry.bump(pid, ENTRY) >= grace:
                    try:
                        await cancel_order(client, oid, pair=pid, side=po.side.lower())
                        print(f"  LIMIT UNFILLED: {pid} order_id={oid} — cancel, cari koin lain", flush=True)
                    except Exception as e:
                        print(f"  Cancel unfilled order failed {pid}: {e}", flush=True)
                    registry.pop(pid, ENTRY)
        except Exception:
            if po.order_id:
                try:
                    await cancel_order(client, po.order_id, pair=pid, side=po.side.lower())
                    print(f"  CANCEL STUCK: {pid} order_id={po.order_id}", flush=True)
                except Exception:
                    pass
            registry.pop(pid, ENTRY)

    for ps in registry.list(SELL):
        pid = ps.pair
        try:
            oid = ps.order_id
            order_info = await get_order(client, oid, pair=pid)
            if not order_info:
                registry.pop(pid, SELL)
                continue
            status = order_info.get("status", "").lower()
            remain = float(order_info.get(f"remain_{pid.split('_')[0]}", 0))
            is_filled = status in ("filled",) or remain <= 0
            if is_filled:
                print(f"  PENDING SELL FILLED: {pid}", flush=True)
                p = next((x for x in S.positions if x["pair"] == pid), None)
                if p:
                    pnl = (float(order_info.get("price", 0)) - p.get("entry_price", 0)) * p["qty"]
                    S.positions.remove(p)
                    persist.save_positions(S.positions)
                    log_trade("sell", float(order_info.get("price", 0)), ps.qty, ps.amount_idr,
                              status="closed", pnl=pnl, reason=f"pending_sell_filled {pid}")
                _sm_cleanup(pid)
                registry.pop(pid, SELL)
                continue
            if status in ("cancelled", "rejected"):
                registry.pop(pid, SELL)
                continue
            if registry.bump(pid, SELL) >= config.SELL_GRACE_CYCLE:
                try:
                    await cancel_order(client, oid, pair=pid, side="sell")
                except Exception:
                    pass
                bid = int(S._latest_ticker_map.get(pid, {}).get("buy", 0))
                if bid > 20:
                    sell_ret = await _sm_place_sell(client, pid, ps.qty, bid)
                    if sell_ret is not None:
                        print(f"  SELL RETRY AT BID: {pid} @ {bid}", flush=True)
                    else:
                        print(f"  SELL RETRY FAILED: {pid}", flush=True)
                registry.pop(pid, SELL)
        except Exception:
            registry.pop(pid, SELL)

    for pid, sm in list(S._position_states.items()):
        if sm["state"] not in ("TP_ACTIVE", "SL_ACTIVE", "TRAILING"):
            continue
        oid = sm.get("tp_order_id") or sm.get("sl_order_id")
        if not oid:
            continue
        try:
            oi = await get_order(client, oid, pair=pid)
            if oi and (oi.get("status", "").lower() in ("filled",) or float(oi.get(f"remain_{pid.split('_')[0]}", 1)) == 0):
                fill_price = float(oi.get("price", 0))
                entry = sm["entry_price"]
                pnl = (fill_price - entry) * sm["qty"]
                p = next((x for x in S.positions if x["pair"] == pid), None)
                if p:
                    S.positions.remove(p)
                    persist.save_positions(S.positions)
                    log_trade("sell", fill_price, sm["qty"], fill_price * sm["qty"], status="closed", pnl=pnl, reason=f"sm_{sm['state']} {pid}")
                    if config.AUTO_COMPOUND:
                        S._realized_pnl_idr += pnl
                    label = "TP" if sm["state"] == "TP_ACTIVE" else "SL"
                    emoji = "🟢" if pnl >= 0 else "🔴"
                    await send_message(f"{emoji} SM {label}: {pid}\n{pnl:+.0f} IDR @ Rp{fill_price:,}")
                    now_rs = time.time()
                    S._realtime_sold_time[pid] = now_rs
                    S._realtime_sold.add(pid)
                    stale_rs = {p for p in S._realtime_sold if now_rs - S._realtime_sold_time.get(p, 0) > 300}
                    S._realtime_sold -= stale_rs
                    print(f"  SM FILLED: {pid} {sm['state']} @ Rp{fill_price:,} ({pnl:+.0f} IDR)", flush=True)
                    if pnl < 0:
                        S._coin_blacklist.add(pid)
                    cd_time = 21600 if pnl >= 0 else 86400
                    S._sm_cooldown[pid] = time.time() + cd_time
                    persist.save_sm_cooldown(S._sm_cooldown)
                    _sm_cleanup(pid)
                continue
            if oi is None:
                print(f"  SM ORDER UNKNOWN: {pid} {sm['state']} oid={oid} — skip (network error)", flush=True)
                continue
            status = oi.get("status", "").lower()
            if status in ("cancelled", "rejected"):
                print(f"  SM ORDER CANCELLED: {pid} {sm['state']} — re-place", flush=True)
                p = next((x for x in S.positions if x["pair"] == pid), None)
                if p:
                    atr = p.get("atr_pct") or S.risk.compute_atr(S._latest_ohlcv_map_1h.get(pid, []))
                    sm["tp_order_id"] = None
                    sm["sl_order_id"] = None
                    if sm["state"] == "TP_ACTIVE":
                        oid = await _sm_place_tp(client, pid, p["qty"], sm["entry_price"], atr)
                        if oid:
                            print(f"  SM TP RE-PLACED: {pid} oid={oid}", flush=True)
                        else:
                            print(f"  SM TP RE-PLACE FAILED: {pid} — PENDING, retry next cycle", flush=True)
                            sm["state"] = "PENDING"
                    elif sm["state"] == "SL_ACTIVE":
                        oid = await _sm_place_sl(client, pid, p["qty"], sm["entry_price"], atr)
                        if oid:
                            print(f"  SM SL RE-PLACED: {pid} oid={oid}", flush=True)
                        else:
                            print(f"  SM SL RE-PLACE FAILED: {pid} — PENDING, retry next cycle", flush=True)
                            sm["state"] = "PENDING"
                    elif sm["state"] == "TRAILING":
                        oid = await _sm_place_sl(client, pid, p["qty"], sm["entry_price"], atr, mult=config.ROTHSCHILD_TRAILING_SL_ATR)
                        if oid:
                            print(f"  SM SL RE-PLACED: {pid} oid={oid}", flush=True)
                        else:
                            print(f"  SM SL RE-PLACE FAILED: {pid} — PENDING, retry next cycle", flush=True)
                            sm["state"] = "PENDING"
                continue
            if sm["state"] == "SL_ACTIVE" and sm.get("sl_price", 0) > 0:
                try:
                    hs_lp = LIVE_TICKERS.get(pid, {}).get("last") or S._latest_ticker_map.get(pid, {}).get("last", 0)
                    hs_pos = next((x for x in S.positions if x["pair"] == pid), None)
                    hs_hold = time.time() - hs_pos["entry_time"] if hs_pos else 0
                    hs_sig = S._latest_all_signals.get(pid, {})
                    hs_raw = hs_sig.get("raw_signal", "HOLD")
                    hs_ema21 = hs_sig.get("ema21")
                    hs_ema50 = hs_sig.get("ema50")
                    hs_bearish = (hs_raw == "SELL") or (hs_ema21 and hs_ema50 and hs_lp < hs_ema21 and hs_lp < hs_ema50)
                    if (hs_lp > 0 and hs_lp < sm["sl_price"] * 0.97
                            and hs_hold > 21600
                            and hs_lp < sm["entry_price"]
                            and hs_bearish):
                        print(f"  CONDITIONAL HARD SL: {pid} — hold {int(hs_hold/3600)}h, signal={hs_raw}, force sell", flush=True)
                        if hs_pos:
                            try:
                                await _sm_cancel(client, oid, pid)
                                bid_h = int(S._latest_ticker_map.get(pid, {}).get("buy", hs_lp))
                                ret_h = await _sm_place_sell(client, pid, hs_pos["qty"], bid_h)
                                if ret_h is not None:
                                    fill_h = float(ret_h.get("price", bid_h))
                                    pnl_h = (fill_h - sm["entry_price"]) * sm["qty"]
                                    S.positions.remove(hs_pos)
                                    persist.save_positions(S.positions)
                                    log_trade("sell", fill_h, sm["qty"], fill_h * sm["qty"], status="closed", pnl=pnl_h, reason=f"sm_hard_sl {pid}")
                                    if config.AUTO_COMPOUND:
                                        S._realized_pnl_idr += pnl_h
                                    await send_message(f"🔴 HARD SL: {pid}\nHold {int(hs_hold/3600)}h, exit Rp{fill_h:,.0f} ({pnl_h:+.0f})")
                                    S._coin_blacklist.add(pid)
                                    cd_h = 86400
                                    S._sm_cooldown[pid] = time.time() + cd_h
                                    persist.save_sm_cooldown(S._sm_cooldown)
                                    _sm_cleanup(pid)
                            except Exception as e:
                                print(f"  HARD SL failed {pid}: {e}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass

    try:
        print("Scanning market for viable pairs...", flush=True)
        await refresh_pair_rules(client)
        viable = await fetch_viable_pairs(client)
        print(f"Found {len(viable)} viable IDR pairs", flush=True)
        current_pairs = {v["pair"] for v in viable}
        new_coins = current_pairs - S.known_pairs
        if new_coins:
            print(f"New coins detected: {', '.join(new_coins)}", flush=True)
        S.known_pairs.update(current_pairs)
        for v in viable:
            S._pair_meta[v["pair"]] = {
                "precision": v.get("price_precision", 1000),
                "vol_precision": v.get("vol_precision", 0),
                "min_traded": v.get("trade_min_traded_currency", 0.0001),
                "min_base": int(v.get("trade_min_base_currency", 10000)),
            }

        live = LIVE_TICKERS.copy()
        if live:
            for v in viable:
                pid = v["pair"]
                if pid in live and v["ticker"].get("last", 0) == 0:
                    v["ticker"] = live[pid]

        if not viable:
            print("No viable pairs. Skipping cycle.", flush=True)
            return

        print("Fetching OHLCV (1h + 4h) with concurrency limit...", flush=True)
        sem = asyncio.Semaphore(config.OHLCV_FETCH_CONCURRENCY)
        ohlcv_map_1h: dict[str, list[dict]] = {}
        ohlcv_map_4h: dict[str, list[dict]] = {}
        ticker_map: dict[str, dict] = {}

        async def fetch_one(v: dict):
            pid = v["pair"]
            async with sem:
                try:
                    o1, o4 = await fetch_ohlcv_both(client, pair=pid)
                    for ohlcv in (o1, o4):
                        cleaned = []
                        for bar in ohlcv:
                            if not isinstance(bar, dict):
                                continue
                            try:
                                c = float(bar.get("close", 0))
                                h = float(bar.get("high", 0))
                                l = float(bar.get("low", 0))
                                o = float(bar.get("open", 0))
                                vol_val = float(bar.get("volume", bar.get("vol", 0)))
                                if any(math.isnan(x) or math.isinf(x) or x <= 0 for x in (c,h,l,o,vol_val)):
                                    continue
                            except Exception:
                                continue
                            cleaned.append(bar)
                        ohlcv[:] = cleaned
                    if len(o1) >= 30:
                        ohlcv_map_1h[pid] = o1
                        ticker_map[pid] = v["ticker"]
                    if len(o4) >= 30:
                        ohlcv_map_4h[pid] = o4
                except Exception as e:
                    print(f"  {pid}: {e}", flush=True)
                    import traceback
                    traceback.print_exc()

        await asyncio.gather(*[fetch_one(v) for v in viable])

        for p in list(S.positions):
            pm = S._pair_meta.get(p["pair"])
            if pm:
                val = p["qty"] * (ticker_map.get(p["pair"], {}).get("last") or LIVE_TICKERS.get(p["pair"], {}).get("last") or p["entry_price"])
                if val < pm["min_base"]:
                    pid = p["pair"]
                    sm = S._position_states.get(pid)
                    if sm:
                        oid = sm.get("tp_order_id") or sm.get("sl_order_id")
                        if oid:
                            try:
                                await _sm_cancel(client, oid, pid)
                            except Exception:
                                pass
                        _sm_cleanup(pid)
                    t = registry.pop(pid, SELL)
                    if t:
                        try:
                            await _sm_cancel(client, t.order_id, pid)
                        except Exception:
                            pass
                    print(f"  CLEANUP: {pid} dust Rp{val:,.0f} < min Rp{pm['min_base']:,} — hapus", flush=True)
                    await send_message(f"🧹 Dust {pid} Rp{int(val):,} dihapus (di bawah min order)")
                    S.positions.remove(p)
                    persist.save_positions(S.positions)

        print(f"Computing signals: {len(ohlcv_map_1h)} pairs (1h) + {len(ohlcv_map_4h)} (4h)...", flush=True)
        all_signals = compute_batch_signals(ohlcv_map_1h, ohlcv_map_4h)

        regime_info = classify_regime(all_signals, ohlcv_map_1h)
        S.regime_history.append(regime_info["regime"])
        if len(S.regime_history) > 12:
            S.regime_history.pop(0)
        hmm_tag = f" HMM:{regime_info.get('hmm_regime', '?')}({regime_info.get('hmm_confidence', 0):.2f})" if regime_info.get('hmm_confidence', 0) > 0 else ""
        _now_t = time.time()
        if regime_info["regime"] != S._prev_regime and S._prev_regime and _now_t - getattr(portfolio_cycle, "_last_regime_notify", 0) > 1800:
            portfolio_cycle._last_regime_notify = _now_t
            await send_message(f"🔄 Regime: {S._prev_regime} → {regime_info['regime']}{hmm_tag}")
        print(f"Regime: {regime_info['regime']}{hmm_tag} | B:{regime_info['buy_ratio']} S:{regime_info['sell_ratio']} "
              f"Score:{regime_info['avg_score']} HC:{regime_info['high_conviction_count']}", flush=True)

        current_regime = regime_info["regime"]

        if current_regime == "BULL":
            S._regime_bull_streak += 1
            S._regime_bear_streak = 0
        elif current_regime == "BEAR":
            S._regime_bear_streak += 1
            S._regime_bull_streak = 0
        elif current_regime in ("SIDEWAYS", "SIDEWAYS_LOW_VOL"):
            S._regime_bull_streak = 0
            S._regime_bear_streak = 0
        else:
            pass

        if S._regime_bull_streak >= config.REGIME_STABILITY_CYCLES and not S._rothschild_active:
            S._rothschild_active = True
            config.ROTHSCHILD_ACTIVE = True
            config.MAX_OPEN_POSITIONS = config.ROTHSCHILD_OPEN_POSITIONS
            config.MAX_POSITION_PCT_PER_ASSET = config.ROTHSCHILD_POSITION_PCT
            print(f"  🚀 ROTHSCHILD AKTIF — {config.ROTHSCHILD_OPEN_POSITIONS} slot @{config.ROTHSCHILD_POSITION_PCT*100:.0f}%", flush=True)
            await send_message(f"🚀 ROTHSCHILD MODE — {config.ROTHSCHILD_OPEN_POSITIONS} slot, pyramid ON")
        elif current_regime != "BULL" and S._rothschild_active and (S._regime_bear_streak >= config.REGIME_STABILITY_BEAR_CYCLES or current_regime == "SIDEWAYS"):
            S._rothschild_active = False
            config.ROTHSCHILD_ACTIVE = False
            config.MAX_OPEN_POSITIONS = 4
            config.MAX_POSITION_PCT_PER_ASSET = 0.25
            print(f"  🛑 ROTHSCHILD OFF — {current_regime} detected, balik ke konservatif 4 slot @25%", flush=True)
            await send_message(f"🛑 ROTHSCHILD OFF — {current_regime} mode, konservatif 4 slot")
        mode_tag = "🔴 R" if S._rothschild_active else "🟢 K"
        kelly_alloc = S.portfolio_risk.kelly_for_regime(current_regime)
        if not S._rothschild_active:
            config.MAX_POSITION_PCT_PER_ASSET = kelly_alloc
        print(f"  Mode: {mode_tag} (regime: {current_regime}, kelly: {kelly_alloc:.0%}, bull streak: {S._regime_bull_streak}, bear: {S._regime_bear_streak})", flush=True)

        actual_idr_balance = 0

        if config.PAPER_TRADING:
            used = sum(p.get("amount_idr", 0) for p in S.positions)
            if S._paper_balance <= 0 or used > 0:
                S._paper_balance = max(0, config.PLAY_CAPITAL_IDR - used)
            actual_idr_balance = S._paper_balance
            _latest_full_balance = {}
            _latest_full_hold = {}
            print(f"  PAPER MODE: simulated balance Rp{actual_idr_balance:,.0f}", flush=True)
            S._position_states.clear()
        elif config.HAS_TRADING_KEYS:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                hold = info.get("balance_hold", {})
                actual_idr_balance = float(bal.get("idr", 0))
                _idr_held = float(hold.get("idr", 0))
                _latest_full_balance = bal
                _latest_full_hold = hold
                for coin in set(list(bal.keys()) + list(hold.keys())):
                    raw_qty = bal.get(coin, 0)
                    qty = float(raw_qty) + float(hold.get(coin, 0))
                    if qty <= 0 or coin == "idr":
                        continue
                    pair = f"{coin}_idr"
                    if pair in config.STABLECOINS or pair in config.SKIP_COINS:
                        continue
                    last_price = ticker_map.get(pair, {}).get("last") or LIVE_TICKERS.get(pair, {}).get("last") or 0

                    old = next((p for p in S.positions if p["pair"] == pair), None)
                    if old:
                        if last_price > 0:
                            coin_value = qty * last_price
                            pair_min = S._pair_meta.get(pair, {}).get("min_base", 10000)
                            if coin_value < pair_min and pair_min > 0:
                                if S._position_states.get(pair):
                                    oid = S._position_states[pair].get("tp_order_id") or S._position_states[pair].get("sl_order_id")
                                    if oid:
                                        await _sm_cancel(client, oid, pair)
                                    _sm_cleanup(pair)
                                print(f"  {pair}: dust Rp{coin_value:,.0f} < min Rp{pair_min:,} — hapus", flush=True)
                                await send_message(f"🧹 Dust {pair} Rp{int(coin_value):,} dihapus (di bawah min order)")
                                S.positions.remove(old)
                                persist.save_positions(S.positions)
                                continue
                        old["qty"] = qty
                        old["amount_idr"] = qty * (old.get("entry_price") or 1)
                        continue

                    if last_price == 0:
                        print(f"  {pair}: belom ada harga — track dulu", flush=True)
                    else:
                        coin_value = qty * last_price
                        if coin_value < 5000:
                            print(f"  {pair}: dust Rp{coin_value:,.0f} < Rp5rb — skip", flush=True)
                            continue
                        pm = S._pair_meta.get(pair, {})
                        min_base = int(pm.get("min_base", 10000))
                        if coin_value < min_base:
                            print(f"  {pair}: dust Rp{coin_value:,.0f} < min Rp{min_base:,} — skip restore", flush=True)
                            if not hasattr(portfolio_cycle, "_dust_equity"):
                                portfolio_cycle._dust_equity = 0
                            portfolio_cycle._dust_equity += coin_value
                            continue

                    db_pos_pairs = {p.get("pair") for p in persist.load_positions()}
                    if pair in db_pos_pairs:
                        continue

                    _saved = S._ext_entry_prices.get(pair, 0)
                    entry_price = _saved if _saved > 0 and last_price > 0 and abs(_saved - last_price) / last_price < 0.2 else (last_price if last_price else _saved)
                    if entry_price > 0:
                        S._ext_entry_prices[pair] = entry_price
                        persist.save_entry_prices(S._ext_entry_prices)
                    if entry_price > 0:
                        print(f"  {pair}: entry_price={entry_price:,}", flush=True)

                    if pair in S._realtime_sold:
                        print(f"  {pair}: skip restore (baru di-SM FILLED)", flush=True)
                        continue
                    if pair in rules.DCA_EXCLUSIVE:
                        print(f"  {pair}: DCA coin — skip restore to positions", flush=True)
                        continue
                    S.positions.append({
                        "pair": pair, "side": "BUY",
                        "entry_price": entry_price,
                        "qty": qty,
                        "amount_idr": qty * (entry_price or 1),
                        "atr_pct": None,
                        "entry_time": time.time(),
                        "entry_mode": "KONSERVATIF",
                    })
                    print(f"  {pair}: restored to positions", flush=True)
                bal_coins = {f"{c}_idr" for c in set(bal.keys()) | set(hold.keys()) if c != "idr" and (float(bal.get(c, 0)) + float(hold.get(c, 0))) > 0}
                for p in list(S.positions):
                    if p["pair"] not in bal_coins:
                        print(f"  CLEANUP: {p['pair']} gak ada di balance — hapus", flush=True)
                        _sm_cleanup(p["pair"])
                        S.positions.remove(p)
                        persist.save_positions(S.positions)
            except Exception as e:
                print(f"Balance fetch error: {e} (using previous balance: Rp{actual_idr_balance:,.0f})", flush=True)
                if not hasattr(portfolio_cycle, "_balance_fail_count"):
                    portfolio_cycle._balance_fail_count = 0
                portfolio_cycle._balance_fail_count += 1
            else:
                fail_count = getattr(portfolio_cycle, "_balance_fail_count", 0)
                if fail_count > 0:
                    portfolio_cycle._balance_fail_count = 0
                    print(f"  Balance recovery — {fail_count} cycle down, checking positions...", flush=True)
                    re_count = 0
                    async with httpx.AsyncClient() as _rc:
                        for pid_sm, sm in list(S._position_states.items()):
                            oid = sm.get("tp_order_id") or sm.get("sl_order_id")
                            if oid:
                                await _sm_cancel(_rc, oid, pid_sm)
                                re_count += 1
                    if re_count:
                        await send_message(f"🔁 Indodax back online — {re_count} stale orders cancelled, SM will re-place")
                        print(f"  Recovery: cancelled {re_count} stale orders", flush=True)

        async def _coin_price(pair: str) -> float:
            lt = LIVE_TICKERS.get(pair, {})
            if lt.get("last"):
                return lt["last"]
            tm = ticker_map.get(pair, {})
            if tm.get("last"):
                return tm["last"]
            try:
                r = await fetch_ticker(client, pair=pair)
                if r and r.get("last"):
                    return r["last"]
            except Exception:
                pass
            return 0

        balance_idr = int(actual_idr_balance)
        paper_prices = await asyncio.gather(*[_coin_price(p["pair"]) for p in S.positions])
        paper_equity = 0
        for p, price in zip(S.positions, paper_prices):
            if price <= 0 and p.get("entry_price"):
                price = p["entry_price"]
            paper_equity += p["qty"] * price
        total_equity = actual_idr_balance + _idr_held + paper_equity
        try:
            for coin in set(list(_latest_full_balance.keys()) + list(_latest_full_hold.keys())):
                if coin == "idr":
                    continue
                c_qty = float(_latest_full_balance.get(coin, 0)) + float(_latest_full_hold.get(coin, 0))
                if c_qty <= 0:
                    continue
                c_pair = f"{coin}_idr"
                if c_pair in {p["pair"] for p in S.positions}:
                    continue
                c_price = float(ticker_map.get(c_pair, {}).get("last", 0) or LIVE_TICKERS.get(c_pair, {}).get("last", 0))
                total_equity += c_qty * c_price
        except Exception:
            pass

        dust_eq = getattr(portfolio_cycle, "_dust_equity", 0)
        if dust_eq:
            total_equity += dust_eq
            portfolio_cycle._dust_equity = 0
        if not S.positions and paper_equity == 0 and total_equity < config.MIN_ORDER_IDR:
            pass
        elif S._prev_equity > 0 and total_equity < S._prev_equity * 0.5 and paper_equity > config.MIN_ORDER_IDR:
            guard_count = getattr(portfolio_cycle, "_equity_guard", 0) + 1
            portfolio_cycle._equity_guard = guard_count
            if guard_count >= 5:
                print(f"  Equity drop confirmed ({guard_count}x) — accept Rp{total_equity:,.0f}", flush=True)
            else:
                print(f"  Equity suspicious ({guard_count}/5): Rp{total_equity:,.0f} vs prev Rp{S._prev_equity:,.0f} — hold", flush=True)
                total_equity = S._prev_equity
        else:
            portfolio_cycle._equity_guard = 0
        max_positions = config.ROTHSCHILD_OPEN_POSITIONS if S._rothschild_active else config.MAX_OPEN_POSITIONS
        if S.cycle_counter == 1:
            S.portfolio_risk.peak_capital = total_equity
            persist.save_peak_capital(total_equity)
            print(f"  Initial equity: Rp{total_equity:,.0f}", flush=True)
        saved_peak = persist.load_peak_capital()
        if saved_peak and saved_peak > S.portfolio_risk.peak_capital:
            S.portfolio_risk.peak_capital = saved_peak
        if total_equity > S.portfolio_risk.peak_capital:
            S.portfolio_risk.peak_capital = total_equity
            persist.save_peak_capital(total_equity)

        base_eq = persist.load_initial_equity() or total_equity
        if config.PAPER_TRADING:
            base_eq = total_equity
        eq_pct = (total_equity - base_eq) / base_eq * 100 if base_eq else 0
        tag = "🔴 R" if S._rothschild_active else "🟢 K"
        hold_tag = " ⏸️" if S._daily_loss_hit_today else ""
        await send_message(
            f"💳 Rp{total_equity:,.0f} ({eq_pct:+.1f}%) {tag}{hold_tag}\n"
            f"Cycle #{S.cycle_counter} | {regime_info['regime']} | {len(S.positions)} pos | Cash: Rp{actual_idr_balance:,.0f}"
        )

        saved_peak = persist.load_today_peak()
        if saved_peak > S.risk.today_peak and 1_000 < saved_peak < 100_000_000_000:
            S.risk.today_peak = saved_peak
            print(f"  Restored today_peak: Rp{S.risk.today_peak:,.0f}", flush=True)

        if S.risk.today_peak > total_equity * 2:
            S.risk.today_peak = total_equity
            persist.save_today_peak(total_equity)
            print(f"  Today peak reset — WD/deposit detected: Rp{total_equity:,.0f}", flush=True)

        daily_limit = S.risk.check_daily_limits(total_equity)
        persist.save_today_peak(S.risk.today_peak)


        _today_d = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
        if persist.load_loss_hit_date() != _today_d:
            S._daily_loss_hit_today = False
            S._greed_used_today = False
            persist.save_daily_loss_hit(False)
            persist.save_loss_hit_date(_today_d)
            print(f"  Daily loss reset — new trading day", flush=True)
        S._latest_regime = regime_info
        S._latest_ticker_map = ticker_map
        S._latest_all_signals = all_signals
        S._latest_ohlcv_map_1h = ohlcv_map_1h
        S._latest_balance = actual_idr_balance
        if total_equity < config.EQUITY_FLOOR_IDR:
            print(f"🏴 EQUITY FLOOR: Rp{total_equity:,.0f} < Rp{config.EQUITY_FLOOR_IDR:,} — no cash, waiting", flush=True)
            return

        for p in list(S.positions):
            pid = p["pair"]
            if pid in rules.DCA_EXCLUSIVE:
                continue
            if config.PAPER_TRADING:
                continue
            if pid not in S._position_states and pid not in S._sm_cooldown:
                atr = p.get("atr_pct")
                if not atr and S._latest_ohlcv_map_1h.get(pid):
                    atr = S.risk.compute_atr(S._latest_ohlcv_map_1h[pid])
                if atr and p["qty"] > 0:
                    try:
                        _sm_init(pid, p["entry_price"], p["qty"], atr, mode="TP_ACTIVE")
                        tp_m = 0.5 if current_regime in ("SIDEWAYS", "SIDEWAYS_LOW_VOL") else config.ATR_TP_MULTIPLIER
                        oid = await _sm_place_tp(client, pid, p["qty"], p["entry_price"], atr, mult=tp_m)
                        if oid:
                            print(f"  SM INIT (pre-loss): {pid} tp_oid={oid}", flush=True)
                        else:
                            S._position_states[pid]["state"] = "PENDING"
                            print(f"  SM PENDING (pre-loss): {pid}", flush=True)
                    except Exception:
                        pass

        if daily_limit == "DAILY_LOSS_LIMIT":
            actual_loss = S.risk.today_peak - total_equity
            if S._greed_used_today and actual_loss < config.DAILY_LOSS_FLOOR_IDR * 2:
                print(f"DAILY LOSS {actual_loss:,.0f} ≥ 15k TAPI GREED — lanjut (max Rp30k)", flush=True)
            else:
                S._daily_loss_hit_today = True
                persist.save_daily_loss_hit(True)
                persist.save_loss_hit_date(_today_d)
                if S._cb_last_loss_date != _today_d:
                    S._cb_consecutive_loss_days += 1
                    S._cb_last_loss_date = _today_d
                    print(f"CIRCUIT BREAKER: consecutive loss day #{S._cb_consecutive_loss_days}", flush=True)
                    persist.save_circuit_breaker({"consecutive_loss_days": S._cb_consecutive_loss_days, "last_loss_date": S._cb_last_loss_date, "triggered_at": S._cb_triggered_at, "active_until": S._cb_active_until})
                    if S._cb_consecutive_loss_days >= config.CIRCUIT_BREAKER_LIMIT:
                        S._cb_triggered_at = time.time()
                        S._cb_active_until = S._cb_triggered_at + config.CIRCUIT_BREAKER_HOURS * 3600
                        persist.save_circuit_breaker({"consecutive_loss_days": S._cb_consecutive_loss_days, "last_loss_date": S._cb_last_loss_date, "triggered_at": S._cb_triggered_at, "active_until": S._cb_active_until})
                        cooldown_until = datetime.datetime.fromtimestamp(S._cb_active_until).strftime("%Y-%m-%d %H:%M")
                        await send_message(f"🛑 CIRCUIT BREAKER: {S._cb_consecutive_loss_days} hari loss berturut — bot stop sampai {cooldown_until} WIB")
                        print(f"🛑 CIRCUIT BREAKER: {S._cb_consecutive_loss_days} consecutive loss days — stop until {cooldown_until}", flush=True)
                print(f"DAILY LOSS LIMIT HIT. Equity: Rp{total_equity:,.0f}. Realtime: TP izin, SL skip.", flush=True)
                return
        else:
            if S._cb_last_loss_date != _today_d:
                S._cb_consecutive_loss_days = 0
                S._cb_last_loss_date = _today_d
                persist.save_circuit_breaker({"consecutive_loss_days": 0, "last_loss_date": "", "triggered_at": S._cb_triggered_at, "active_until": S._cb_active_until})

        if S.portfolio_risk.check_portfolio_stop(total_equity):
            actual_dd = (S.portfolio_risk.peak_capital - total_equity) / S.portfolio_risk.peak_capital * 100
            print(f"DRAWDOWN {actual_dd:.0f}% > {abs(config.PORTFOLIO_STOP_LOSS_PCT)*100:.0f}% — "
                  f"Equity: Rp{total_equity:,.0f}, stopping entry", flush=True)
            await send_message(f"🛑 DRAWDOWN {actual_dd:.0f}% — stop entry, TP-only")
            S._daily_loss_hit_today = True
            persist.save_daily_loss_hit(True)

        for p in S.positions:
            last = ticker_map.get(p["pair"], {}).get("last", p.get("current_price") or p.get("entry_price") or 0)
            p["pnl_pct"] = round(pnl_pct(p.get("entry_price") or 0, last, p["side"]), 2) if last else 0

        current_positions_info = [
            {
                "pair": p["pair"],
                "side": p["side"],
                "entry_price": p.get("entry_price") or 0,
                "qty": p["qty"],
                "pnl_pct": p.get("pnl_pct", 0),
                "current_value": p["qty"] * ticker_map.get(p["pair"], {}).get("last", 0),
            }
            for p in S.positions
        ]

        if len(ohlcv_map_1h) >= 5 and (not S.hmm_detector.trained or S.cycle_counter % config.HMM_RETRAIN_INTERVAL == 0):
            try:
                S.hmm_detector.fit(ohlcv_map_1h)
                if S.hmm_detector.trained:
                    print(f"HMM regime detector trained ({config.HMM_N_STATES} states)", flush=True)
                    if S.cycle_counter <= 2:
                        await send_message(f"🧠 HMM: {regime_info.get('regime','?')} (confidence {regime_info.get('hmm_confidence',0):.0%})")
            except Exception as e:
                print(f"HMM train error: {e}", flush=True)
                S.hmm_detector.trained = False

        S._prev_equity = total_equity
        S._prev_regime = regime_info["regime"]

        await S.grid_mini.check_sell_fills(client)

        saved_curve = persist.load_equity_curve()
        saved_curve.append(total_equity)
        if len(saved_curve) > 200:
            saved_curve = saved_curve[-200:]
        persist.save_equity_curve(saved_curve)

        book_pressure_map = {}
        top_sigs = sorted(all_signals.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True)[:5]
        for pid, _ in top_sigs:
            try:
                ob = await fetch_orderbook(client, pair=pid, depth=5)
                if ob:
                    book_pressure_map[pid] = ob
            except Exception:
                pass

        decision = rules.decide(
            all_signals, ticker_map, LIVE_TICKERS, S.positions,
            actual_idr_balance, total_equity, regime_info, ohlcv_map_1h,
            S._coin_blacklist, S._pair_meta, book_pressure_map=book_pressure_map,
            sm_cooldown=S._sm_cooldown,
        )

        pair_sigs = pairs.compute_all_pairs(ohlcv_map_1h) if config.CORRELATION_PAIRS else []
        if pair_sigs:
            pair_trades = pairs.pair_signals_to_trades(pair_sigs, ticker_map, LIVE_TICKERS)
            decision["trades"].extend(pair_trades)
            if pair_trades:
                print(f"  Pair trades added: {len(pair_trades)} ({pair_sigs[0]['reason'][:40]})", flush=True)
        print(f"Decision: {decision['decision']} | {decision['reasoning'][:80]}", flush=True)

        if actual_idr_balance < config.MIN_ORDER_IDR:
            print(f"Cash Rp{actual_idr_balance:,.0f} < min Rp{config.MIN_ORDER_IDR:,}. Skipping buys.", flush=True)
            balance_idr = 0
        balance_idr = max(balance_idr, config.MIN_ORDER_IDR)
        print(f"Balance: Rp{balance_idr:,}", flush=True)

        hour_wib = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).hour
        if 0 <= hour_wib < 8:
            session_tag = "🇯🇵 Asia"
            session_mult = 0.7
        elif 8 <= hour_wib < 16:
            session_tag = "🇪🇺 Euro"
            session_mult = 1.0
        else:
            session_tag = "🇺🇸 US"
            session_mult = 1.3
        balance_idr = int(balance_idr * session_mult)
        balance_idr = min(balance_idr, max(actual_idr_balance, 0))
        print(f"Session: {session_tag} ({hour_wib:02d} WIB) — size: {session_mult:.1f}x (capped Rp{balance_idr:,})", flush=True)

        log_decision("PORTFOLIO", decision.get("decision", "HOLD"),
                     decision.get("reasoning", ""),
                     executed=len(decision.get("trades", [])) > 0)

        for p in list(S.positions):
            pid = p["pair"]
            if pid in rules.DCA_EXCLUSIVE:
                continue
            sm = S._position_states.get(pid)
            if sm:
                if sm["state"] == "PENDING":
                    atr = p.get("atr_pct") or S.risk.compute_atr(ohlcv_map_1h.get(pid, []))
                    oid = await _sm_place_tp(client, pid, p["qty"], p["entry_price"], atr)
                    if oid:
                        print(f"  SM RETRY OK: {pid} tp_oid={oid}", flush=True)
                continue
            if pid in S._sm_cooldown and S._sm_cooldown[pid] > time.time():
                continue
            elif pid in S._sm_cooldown:
                del S._sm_cooldown[pid]
            atr = p.get("atr_pct") or S.risk.compute_atr(ohlcv_map_1h.get(pid, []))
            if atr and p["qty"] > 0:
                _sm_init(pid, p["entry_price"], p["qty"], atr, mode="TP_ACTIVE")
                tp_mult = 0.5 if current_regime in ("SIDEWAYS", "SIDEWAYS_LOW_VOL") else config.ATR_TP_MULTIPLIER
                oid = await _sm_place_tp(client, pid, p["qty"], p["entry_price"], atr, mult=tp_mult)
                if oid:
                    print(f"  SM INIT: {pid} tp_oid={oid} entry={p['entry_price']:,.0f} atr={atr:.2f}% tp_mult={tp_mult:.1f}", flush=True)
                else:
                    S._position_states[pid]["state"] = "PENDING"
                    print(f"  SM PENDING: {pid} — TP fail, retry next cycle", flush=True)

        for pid_sm, sm in list(S._position_states.items()):
            if sm["state"] != "TP_ACTIVE":
                continue
            lp_sm = ticker_map.get(pid_sm, {}).get("last") or LIVE_TICKERS.get(pid_sm, {}).get("last", 0)
            if lp_sm <= 0:
                continue
            p_sm = next((x for x in S.positions if x["pair"] == pid_sm), None)
            if not p_sm:
                continue
            entry_sm = sm["entry_price"]
            atr_sm = sm.get("atr_pct", 1.0)
            if S._latest_ohlcv_map_1h.get(pid_sm) and len(S._latest_ohlcv_map_1h[pid_sm]) >= 20:
                recalc = S.risk.compute_atr(S._latest_ohlcv_map_1h[pid_sm])
                if recalc > atr_sm:
                    atr_sm = recalc
            bear_regime = S._latest_regime.get("regime", "") in ("BEAR",)
            if bear_regime and sm["state"] == "TP_ACTIVE" and sm.get("tp_order_id"):
                bear_bid = int(ticker_map.get(pid_sm, {}).get("buy", 0) or lp_sm)
                sm_tp = sm.get("tp_price", 0)
                if bear_bid > entry_sm and sm_tp > bear_bid * 1.005:
                    print(f"  SM BEAR TP: {pid_sm} — tighten TP (was Rp{sm_tp:,} → bid Rp{bear_bid:,})", flush=True)
                    async with httpx.AsyncClient() as _tc:
                        await _sm_cancel(_tc, sm["tp_order_id"], pid_sm)
                        sm["tp_order_id"] = None
                        sm["tp_price"] = 0
                        new_oid = await _sm_place_sell(_tc, pid_sm, p_sm["qty"], bear_bid)
                        if new_oid:
                            sm["tp_order_id"] = new_oid
                            sm["tp_price"] = bear_bid
            sl_level_sm = entry_sm * (1 - max(atr_sm, 0.5) * config.ATR_SL_MULTIPLIER / 100)
            if lp_sm <= sl_level_sm:
                async with httpx.AsyncClient() as _tc:
                    if sm.get("tp_order_id"):
                        await _sm_cancel(_tc, sm["tp_order_id"], pid_sm)
                    oid = await _sm_place_sl(_tc, pid_sm, p_sm["qty"], entry_sm, atr_sm)
                    if oid:
                        sm["sl_order_id"] = oid
                        sm["tp_order_id"] = None
                        sm["state"] = "SL_ACTIVE"
                        msg = f"⚡ SM → SL: {pid_sm}\nPrice Rp{lp_sm:,.0f} hit SL Rp{int(sl_level_sm):,}"
                        print(f"  SM CYCLE → SL: {pid_sm} @ Rp{int(sl_level_sm):,} (price {lp_sm:,.0f})", flush=True)
                        await send_message(msg)

        sl_hits = []
        for p in list(S.positions):
            if p["pair"] in S._realtime_sold:
                continue
            if registry.has(p["pair"], SELL):
                continue
            if S._position_states.get(p["pair"]) or p["pair"] in S._sm_cooldown:
                continue
            mentry = S._momentum_entry_time.get(p["pair"], 0)
            if mentry > 0 and time.time() - mentry < 120:
                continue
            last = ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            atr_val = p.get("atr_pct") or S.risk.compute_atr(ohlcv_map_1h.get(p["pair"], []))
            result = S.risk.check_sl_tp(p["entry_price"], last, p["side"], pair=p["pair"], atr_pct=atr_val, entry_mode=p.get("entry_mode", "KONSERVATIF"))
            if not result and atr_val:
                atr_sl = atr_val * config.ATR_SL_MULTIPLIER
                dyn_sl = p["entry_price"] * (1 - atr_sl / 100) if p["side"] == "BUY" else p["entry_price"] * (1 + atr_sl / 100)
                if (p["side"] == "BUY" and last <= dyn_sl) or (p["side"] == "SELL" and last >= dyn_sl):
                    result = "ATR_SL"
            if result == "PYRAMID_TRIGGER":
                cash = actual_idr_balance
                pyr_amount = int(max(config.MIN_ORDER_IDR, cash * config.ROTHSCHILD_PYRAMID_MULT))
                if pyr_amount >= config.MIN_ORDER_IDR:
                    pyr_price = last
                    pyr_qty = pyr_amount / pyr_price
                    try:
                        pyr_price_adj = int(pyr_price * config.PYRAMID_PRICE_ADJ)
                        pyr_order = await place_order(client, "buy", pyr_price_adj, pyr_amount, pair=p["pair"], order_type="market")
                        if pyr_order.get("order_id") or pyr_order.get("receive_rp"):
                            coin_name = p["pair"].split("_")[0]
                            pyr_fill = float(pyr_order.get(f"receive_{coin_name}", 0)) or pyr_qty
                            pyr_spend = float(pyr_order.get("spend_rp", 0)) or pyr_amount
                            add_position(S.positions, p["pair"], p["side"], pyr_spend / pyr_fill if pyr_fill else p["entry_price"],
                                         pyr_fill, pyr_spend, p.get("atr_pct"), time.time(),
                                         p.get("entry_mode", "KONSERVATIF"))
                            actual_idr_balance -= pyr_spend
                            persist.save_positions(S.positions)
                            print(f"  PYRAMID: added {pyr_fill:.6f} @ {pyr_price:,.0f} (Rp{pyr_spend:,.0f})", flush=True)
                            await send_message(f"🔺 PYRAMID: BUY more {p['pair']}\n+{pyr_fill:.6f} @ Rp{pyr_price:,.0f}")
                    except Exception as e:
                        print(f"  Pyramid order failed {p['pair']}: {e}", flush=True)
                continue

            if result:
                pnl = (last - p["entry_price"]) * p["qty"]
                if p["side"] == "SELL":
                    pnl = (p["entry_price"] - last) * p["qty"]
                dust_value = p["qty"] * last
                pair_min = S._pair_meta.get(p["pair"], {}).get("min_base", config.MIN_ORDER_IDR)
                if dust_value < pair_min:
                    print(f"  {p['pair']}: dust Rp{dust_value:,.0f} < min Rp{pair_min:,} — hapus tracking", flush=True)
                    _sm_cleanup(p["pair"])
                    S.positions.remove(p)
                    persist.save_positions(S.positions)
                    continue
                sl_hits.append(f"{p['pair']} {result}: {pnl:+.0f} IDR")
                if result != "INITIAL_SL":
                    S._cooldown[p["pair"]] = time.time()
                    print(f"COOLDOWN: {p['pair']} set for 12h", flush=True)
                    await send_message(f"⏳ {p['pair']} cooldown 12 jam")
                if not config.PAPER_TRADING and config.HAS_TRADING_KEYS:
                    try:
                        coin_name = p["pair"].split("_")[0]
                        sl_qty = p["qty"]
                        try:
                            info_sl = await get_balance(client)
                            real_sl = float(info_sl.get("balance", {}).get(coin_name, 0))
                            if real_sl > 0:
                                sl_qty = real_sl
                        except Exception:
                            pass
                        sell_bid = int(ticker_map.get(p["pair"], {}).get("buy", last))
                        sell_price = quantize_price(int(sell_bid * (1 + config.MAKER_SLIPPAGE)), p["pair"], "sell")
                        sj_ret = None
                        sj_err = "unknown"
                        try:
                            sj_ret = await place_order(client, "sell", sell_price, sl_qty * sell_price,
                                                       pair=p["pair"], order_type="limit", qty=sl_qty)
                        except Exception as e:
                            sj_err = str(e)
                        if sj_ret is not None:
                            remain = float(sj_ret.get(f"remain_{coin_name}", 0))
                            if remain > 0:
                                print(f"  SELL UNFILLED: {p['pair']} — track sampe keisi (max {config.SELL_GRACE_CYCLE} cycle)", flush=True)
                                order_id = sj_ret.get("order_id")
                                if order_id:
                                    registry.add(p["pair"], SELL, order_id, side="sell",
                                                 qty=sl_qty, price=sell_price, amount_idr=sl_qty * last)
                                continue
                            print(f"  SOLD {p['pair']} via maker", flush=True)
                            _sm_cleanup(p["pair"])
                            S.positions.remove(p)
                            persist.save_positions(S.positions)
                            sell_value = last * p["qty"]
                            log_trade("sell", last, p["qty"], sell_value,
                                      status="closed", pnl=pnl, reason=f"{result} {p['pair']}")
                            if config.AUTO_COMPOUND:
                                S._realized_pnl_idr += pnl
                        else:
                            print(f"  Sell {p['pair']} failed: {sj_err} — CIO will decide", flush=True)
                    except Exception as e:
                        print(f"  Auto-sell failed {p['pair']}: {e}", flush=True)

        for sl_hit in sl_hits:
            if "SL_HIT" in sl_hit or "TRAILING_SL" in sl_hit or "INITIAL_SL" in sl_hit or "ATR_SL" in sl_hit:
                pair = sl_hit.split(" ")[0].replace(":", "")
                if S._rothschild_active:
                    S._cooldown[pair] = time.time() + 3600
                    print(f"COOLDOWN: {pair} 60 menit (Rothschild mode)", flush=True)
                else:
                    S._coin_blacklist.add(pair)
                    print(f"BLACKLIST: {pair} added (hit stop loss)", flush=True)
                    await send_message(f"⛔ {pair} blacklist (kena SL)")
        if len(S._coin_blacklist) > 20:
            oldest = next(iter(S._coin_blacklist))
            S._coin_blacklist.discard(oldest)
        persist.save_blacklist(S._coin_blacklist)
        persist.save_cooldown(S._cooldown)
        persist.save_sm_cooldown(S._sm_cooldown)
        now_rs = time.time()
        S._realtime_sold -= {p for p in S._realtime_sold if now_rs - S._realtime_sold_time.get(p, 0) > 300}

        if sl_hits:
            await send_message("SL/TP triggered:\n" + "\n".join(sl_hits))

        today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
        if getattr(portfolio_cycle, "_last_report_date", "") != today_str:
            portfolio_cycle._last_report_date = today_str
            sells = [t for t in get_trades_by_period("day") if t["side"] == "sell" and t["pnl"] is not None]
            total_pnl = sum(t["pnl"] for t in sells)
            wins = len([t for t in sells if t["pnl"] > 0])
            losses = len([t for t in sells if t["pnl"] <= 0])
            buys_today = len([t for t in get_trades_by_period("day") if t["side"] == "buy"])
            wr = wins / max(wins + losses, 1) * 100
            fee_buy = buys_today * 25000 * config.TAKER_FEE_PCT
            fee_sell = sum(abs(t.get("amount_idr", 0) or 0) for t in sells) * config.MAKER_FEE_PCT
            await send_message(
                f"📊 HARIAN\n"
                f"Buy {buys_today}x | Sell {wins+losses}x ({wins}W/{losses}L | {wr:.0f}%)\n"
                f"PnL: Rp{total_pnl:+,.0f} | Fee: Rp{fee_buy + fee_sell:,.0f}\n"
                f"Net: Rp{total_pnl - fee_buy - fee_sell:+,.0f}"
            )

        trades = decision.get("trades", [])
        trades_today = get_trade_count_today()
        if trades_today >= config.MAX_DAILY_TRADES:
            print(f"MAX TRADES/DAY ({config.MAX_DAILY_TRADES}) reached. Skipping buys.", flush=True)
            trades = [t for t in trades if t.get("action") != "BUY"]
            if not trades:
                print(f"Max trades/day — no sells to execute. Sleeping.", flush=True)
                persist.save_initial_equity(total_equity)
                if S.positions and config.HAS_TRADING_KEYS:
                    pair_str = ",".join(p["pair"] for p in S.positions[:5])
                    await refresh_deadman(client, pair_str)
                return

        all_held = {p["pair"] for p in S.positions}
        bot_pair_set = {p["pair"] for p in S.positions}
        trades = [t for t in trades if t.get("action") != "SELL" or t["pair"] in all_held]
        for t in list(trades):
            if t.get("action") == "SELL":
                sell_pair = t["pair"]
                match = next((p for p in S.positions if p["pair"] == sell_pair), None)
                if not match:
                    continue
                price_now = LIVE_TICKERS.get(sell_pair, {}).get("last") or ticker_map.get(sell_pair, {}).get("last", 0)
                entry = match.get("entry_price", 0)
                pnl = (price_now - entry) / entry * 100 if entry else 0
        if S.cycle_counter <= 1:
            if any(t.get("action") == "SELL" for t in decision.get("trades", [])):
                print("STARTUP GUARD: blocked CIO sells (positions restored from balance)", flush=True)
            trades = [t for t in trades if t.get("action") != "SELL"]
        trades = [t for t in trades if t.get("action") != "BUY" or (t["pair"] not in S._coin_blacklist and (t["pair"] not in S._cooldown or time.time() >= S._cooldown.get(t["pair"], 0)) and (t["pair"] not in S._sm_cooldown or time.time() >= S._sm_cooldown.get(t["pair"], 0)))]
        if config.SKIP_COINS:
            trades = [t for t in trades if t.get("action") != "BUY" or t["pair"] not in config.SKIP_COINS]
        if S._coin_blacklist:
            blocked = [t for t in decision.get("trades", []) if t.get("action") == "BUY" and t["pair"] in S._coin_blacklist]
            if blocked:
                print(f"BLACKLIST: Skipped BUY for {', '.join(t['pair'] for t in blocked)}", flush=True)
        grid_active = {g.pair for g in S.grid_mini.instances}
        if grid_active:
            trades = [t for t in trades if not (t.get("action") == "BUY" and t["pair"] in grid_active)]
            if any(t["pair"] in grid_active for t in trades if t.get("action") == "BUY"):
                print(f"  GRID MINI ACTIVE: skip buy untuk {grid_active}", flush=True)
        for p in list(S.positions):
            last_p = await _coin_price(p["pair"]) or LIVE_TICKERS.get(p["pair"], {}).get("last") or 0
            if last_p == 0:
                try:
                    t = await fetch_ticker(client, pair=p["pair"])
                    if t: last_p = t.get("last", 0)
                except Exception:
                    pass
            if last_p <= 0 and p["pair"] not in S._realtime_sold and p["pair"] not in S._position_states and p["pair"] not in S._sm_cooldown:
                print(f"  FORCE SELL {p['pair']}: harga 0 — force close (tidak SM managed)", flush=True)
                trades.append({"pair": p["pair"], "action": "SELL", "allocation_pct": 100, "reason": "Force close no price"})

        selling_pairs = {t["pair"] for t in trades if t.get("action") == "SELL"}
        extra_buys = [t for t in trades if t.get("action") == "BUY" and t["pair"] in all_held]
        new_buys = [t for t in trades if t.get("action") == "BUY" and t["pair"] not in all_held]
        non_dca_held = {p for p in all_held if p not in rules.DCA_EXCLUSIVE}
        slots_left = max(0, max_positions - len(non_dca_held - selling_pairs))
        if len(new_buys) > slots_left:
            trades = [t for t in trades if t.get("action") == "SELL"] + extra_buys + new_buys[:slots_left]
            print(f"Limited new buys to {slots_left} (max {max_positions} unique, equity Rp{total_equity:,.0f})", flush=True)

        if not trades:
            print(f"Cycle done in {int(time.time() - _t0)}s. Sleeping.", flush=True)
            persist.save_initial_equity(total_equity)
            if S.positions and config.HAS_TRADING_KEYS:
                pair_str = ",".join(p["pair"] for p in S.positions[:5])
                await refresh_deadman(client, pair_str)
            return

        valid_trades = S.portfolio_risk.validate_allocation(trades, current_positions_info, balance_idr)
        if not valid_trades:
            print("No valid trades after risk checks.", flush=True)
            print(f"Cycle done in {int(time.time() - _t0)}s. Sleeping.", flush=True)
            return

        correlated = correlation_risk(S.positions + [{"pair": t["pair"], "entry_price": 0} for t in valid_trades if t.get("action") == "BUY"], ticker_map)
        if correlated:
            corr_pairs = set()
            for c in correlated:
                for pair in c.split("+"):
                    corr_pairs.add(pair)
            valid_trades = [t for t in valid_trades if t["action"] == "SELL" or t["pair"] not in corr_pairs or len(corr_pairs) <= 1]
            if not valid_trades:
                print("All BUY trades eliminated by correlation check.", flush=True)
                print(f"Cycle done in {int(time.time() - _t0)}s. Sleeping.", flush=True)
                return

        executed_trades = []
        for t in valid_trades:
            pid = t["pair"]
            action = t["action"]
            alloc = t["allocation_pct"]

            match = next((p for p in S.positions if p["pair"] == pid), None)
            if action == "SELL" and match:
                sell_qty_raw = match["qty"]
                if not config.PAPER_TRADING and config.HAS_TRADING_KEYS:
                    try:
                        info_now = await get_balance(client)
                        real_bal = float(info_now.get("balance", {}).get(pid.split("_")[0], 0))
                        if real_bal > 0:
                            sell_qty_raw = real_bal
                    except Exception:
                        pass
                qty = sell_qty_raw
                ticker = ticker_map.get(pid, {})
                price = ticker.get("buy", 0)
                if not price or price < 20:
                    continue
                amount = qty * price
                t["entry_price"] = match.get("entry_price", 0)
                t["exec_price"] = price
            else:
                amount = balance_idr * (alloc / 100)
                ticker = ticker_map.get(pid, {})
                price = ticker.get("sell" if action == "BUY" else "buy", 0)
                if not price or price < 20:
                    continue
                qty = amount / price

            ohlcv = ohlcv_map_1h.get(pid)
            atr_pct = S.risk.compute_atr(ohlcv) if ohlcv else None
            if action == "BUY" and atr_pct:
                raw_atr = S.risk.compute_atr(ohlcv, clamped=False)
                vol_idr = float(ticker.get("vol_idr", 0))
                if raw_atr < 0.5:
                    print(f"  {pid}: ATR {raw_atr:.1f}% < 1.5 — skip (terlalu stabil)", flush=True)
                    continue
                if raw_atr > 25.0 or vol_idr < 500_000_000:
                    print(f"  {pid}: ATR {raw_atr:.1f}% vol Rp{vol_idr:,.0f} — skip (terlalu berisiko)", flush=True)
                    continue
            if not S.risk.is_profit_viable(price, qty, action, atr_pct=atr_pct):
                print(f"  {pid}: skipped - fees eat profit", flush=True)
                continue

            print(f"  {action} {pid} @ {price} | Rp{amount:,.0f} ({qty:.6f}) | alloc: {alloc}%", flush=True)

            tp_limit_price = 0
            if atr_pct and action == "BUY":
                sl, tp = S.risk.get_sl_tp(price, action, atr_pct)
                tp_limit_price = int(tp)
                print(f"  ATR: {atr_pct}% | SL: {sl} | TP: {tp}", flush=True)

            if action == "SELL" and S._position_states.get(pid) and not config.PAPER_TRADING:
                print(f"  SKIP SELL {pid}: SM aktif, exit via state machine", flush=True)
                continue
            else:
                ot = "maker_first" if config.MAKER_FIRST and action == "BUY" else "market"
            try:
                order = await place_order(client, action.lower(), price, amount,
                                           pair=pid, order_type=ot,
                                           qty=qty if action == "SELL" else None)
            except Exception as e:
                err_str = str(e)
                print(f"  Order failed {pid}: {err_str}", flush=True)
                now_t = time.time()
                last_err = S._order_error_cooldown.get(pid, 0)
                if now_t - last_err > 1800:
                    S._order_error_cooldown[pid] = now_t
                    await send_message(f"Order failed {pid}: {err_str}")
                if action == "SELL" and "insufficient" in err_str.lower():
                    S.positions = [p for p in S.positions if p["pair"] != pid]
                    persist.save_positions(S.positions)
                    _sm_cleanup(pid)
                    print(f"  REMOVED {pid} from tracking (sell failed)", flush=True)
                continue

            is_pending_maker = action == "BUY" and ot == "maker_first" and order.get("order_id") and float(order.get("remain_rp", order.get("remain_idr", 0)) or 0) > 0
            if is_pending_maker:
                registry.add(pid, ENTRY, order["order_id"], side="buy", price=price, qty=qty,
                             amount_idr=amount, is_maker=True,
                             atr_pct=(atr_pct if ohlcv else None),
                             client_order_id=order.get("client_order_id", ""))
                print(f"  MAKER ORDER PLACED: {pid} (order_id={order['order_id']})", flush=True)
                continue

            log_trade(action.lower(), price, qty, amount,
                      order_type="market",
                      status="simulated" if config.PAPER_TRADING else "placed",
                       reason=f"{t.get('reason', '')} {pid}")

            coin_name = pid.split("_")[0]
            if action == "BUY":
                actual_qty = float(order.get(f"receive_{coin_name}", 0)) or qty
                actual_spend = float(order.get("spend_rp", 0)) or amount
                actual_price = actual_spend / actual_qty if actual_qty else price
                t["entry_price"] = actual_price
                t["exec_price"] = actual_price
                actual_idr_balance -= actual_spend
                if config.PAPER_TRADING:
                    S._paper_balance = actual_idr_balance
                S._latest_balance = actual_idr_balance
                add_position(S.positions, pid, action, actual_price, actual_qty, actual_spend,
                             atr_pct if ohlcv else None, time.time(),
                             "ROTHSCHILD" if S._rothschild_active else "KONSERVATIF")
                persist.save_positions(S.positions)
                S._ext_entry_prices[pid] = actual_price
                persist.save_entry_prices(S._ext_entry_prices)
            elif action == "SELL":
                actual_received = float(order.get("receive_rp", 0)) or amount
                actual_qty = float(order.get(f"spend_{coin_name}", 0)) or qty
                actual_sell_price = actual_received / actual_qty if actual_qty else price
                t["exec_price"] = actual_sell_price
                actual_idr_balance += actual_received
                if config.PAPER_TRADING:
                    S._paper_balance = actual_idr_balance
                S._latest_balance = actual_idr_balance
                S.positions = [p for p in S.positions if p["pair"] != pid]
                persist.save_positions(S.positions)
                _sm_cleanup(pid)
                sell_entry = t.get("entry_price", 0) or 0
                sell_cost = sell_entry * actual_qty
                sell_pnl_idr = actual_received - sell_cost
                if sell_pnl_idr > 0:
                    S._cio_stats["wins"] += 1
                else:
                    S._cio_stats["losses"] += 1
                if config.AUTO_COMPOUND:
                    S._realized_pnl_idr += sell_pnl_idr
            pnl_trade = (t.get("exec_price", 0) - t.get("entry_price", 0)) / t.get("entry_price", 1) * 100 if t.get("entry_price") else 0
            S._recent_actions.append({
                "time": time.time(), "action": action, "pair": pid,
                "price": t.get("exec_price", price), "pnl": round(pnl_trade, 2),
                "qty": actual_qty if action == "SELL" else (actual_qty if action == "BUY" else qty),
            })
            if len(S._recent_actions) > 50:
                S._recent_actions.pop(0)
            executed_trades.append(t)

            if action == "SELL":
                registry.pop(pid, ENTRY)

        if executed_trades:
            S._cio_stats["total_decisions"] += 1
            for t in executed_trades:
                S._cio_stats["buys" if t.get("action") == "BUY" else "sells"] += 1

            msg_lines = [f"{'[PAPER] ' if config.PAPER_TRADING else ''}FMA ALPHA QUANT LABS — EKSEKUSI"]
            for t in executed_trades:
                pid = t["pair"]
                entry = t.get("entry_price", 0)
                exec_price = t.get("exec_price", 0)
                pnl_pct_real = 0
                if entry and exec_price:
                    pnl_pct_real = (exec_price - entry) / entry * 100
                reason = t.get("reason", "")
                if pnl_pct_real <= -0.1 and any(w in reason.lower() for w in ["take profit", "tp", "profit", "gain"]):
                    reason = f"Cut loss ({pnl_pct_real:+.1f}%)"
                elif pnl_pct_real > 0.1 and any(w in reason.lower() for w in ["cut loss", "stop loss", "loss"]):
                    reason = f"Take profit ({pnl_pct_real:+.1f}%)"
                elif pnl_pct_real < -0.1:
                    reason = f"Cut loss ({pnl_pct_real:+.1f}%)"
                elif pnl_pct_real > 0.1:
                    reason = f"Take profit ({pnl_pct_real:+.1f}%)"
                pnl_tag = f" [{pnl_pct_real:+.1f}%]" if t.get("entry_price") and pnl_pct_real else ""
                action_label = "BUY" if t.get("action") == "BUY" else ("SELL" if t.get("action") == "SELL" else t.get("action", ""))
                msg_lines.append(f"{action_label} {pid}{pnl_tag} ({t['allocation_pct']}%) — {reason[:80]}")
            msg_lines.append(f"Total posisi: {len(S.positions)} | Cash: Rp{actual_idr_balance:,.0f} (budget: Rp{balance_idr:,})")
            await send_message("\n".join(msg_lines))
            print(f"Portfolio: {len(S.positions)} total positions | Cash: Rp{actual_idr_balance:,.0f} (budget: Rp{balance_idr:,})", flush=True)

        seen = set()
        deduped = []
        for p in S.positions:
            if p["pair"] not in seen:
                seen.add(p["pair"])
                deduped.append(p)
        if len(deduped) != len(S.positions):
            S.positions[:] = deduped
            persist.save_positions(S.positions)
            print(f"DEDUP: removed {len(S.positions) - len(deduped)} duplicate positions", flush=True)

        if S.positions and config.HAS_TRADING_KEYS:
            pair_str = ",".join(p["pair"] for p in S.positions[:5])
            await refresh_deadman(client, pair_str)

        if S.cycle_counter % 10 == 0 and config.HAS_TRADING_KEYS:
            try:
                orders_map = await get_open_orders(client)
                tracked = {p["pair"] for p in S.positions} | registry.tracked_pairs()
                if isinstance(orders_map, dict):
                    for opair, olist in orders_map.items():
                        if opair in tracked or not isinstance(olist, list):
                            continue
                        for o in olist:
                            oid = o.get("order_id")
                            # pair tanpa position bukan bukti order ini yatim:
                            # grid/DCA/SM menyimpan order resting tanpa position
                            if not oid or registry.owns(oid):
                                continue
                            await cancel_order(client, oid, pair=opair, side=o.get("type", "buy"))
                            print(f"CLEANUP: cancelled orphan {opair} {o.get('type')} (order_id={oid})", flush=True)
            except Exception as e:
                print(f"Orphan cleanup error: {e}", flush=True)

        await S.grid_mini.check_sell_fills(client)
        await S.grid_mini.check_fills_and_place_tp(client, ticker_map)
        grid_positions = {p["pair"] for p in S.positions}
        grid_blacklist = S._coin_blacklist | set(config.SKIP_COINS)
        grid_cooldown = {k for k in S._sm_cooldown if S._sm_cooldown[k] > time.time()} | {k for k in S._cooldown if S._cooldown[k] > time.time()}
        grid_max_trades = get_trade_count_today() >= config.MAX_DAILY_TRADES
        await S.grid_mini.scan_and_place(ticker_map, ohlcv_map_1h, current_regime, actual_idr_balance,
                                        existing_positions=grid_positions, blacklisted=grid_blacklist,
                                        cooldown_set=grid_cooldown,
                                        daily_loss_hit=S._daily_loss_hit_today, max_trades_reached=grid_max_trades)
        await S.grid_mini.cleanup_stale()
        S.grid_mini.save_instances()

        if config.AUTO_COMPOUND and S._realized_pnl_idr != 0:
            old_cap = config.PLAY_CAPITAL_IDR
            config.PLAY_CAPITAL_IDR = max(
                config.PLAY_CAPITAL_IDR + S._realized_pnl_idr,
                config.MIN_ORDER_IDR
            )
            if config.COMPOUND_CAP_IDR > 0:
                config.PLAY_CAPITAL_IDR = min(config.PLAY_CAPITAL_IDR, config.COMPOUND_CAP_IDR)
            sign = "+" if S._realized_pnl_idr >= 0 else ""
            print(f"COMPOUND: {sign}Rp{S._realized_pnl_idr:,.0f} (Rp{old_cap:,.0f} → Rp{config.PLAY_CAPITAL_IDR:,.0f})", flush=True)
            S._realized_pnl_idr = 0.0

    except Exception as e:
        print(f"Portfolio cycle error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            now_e = time.time()
            if now_e - S._order_error_cooldown.get("cycle_error", 0) > 1800:
                S._order_error_cooldown["cycle_error"] = now_e
                await send_message(f"Portfolio cycle error: {e}")
        except Exception:
            pass
    finally:
        S._cycle_last_end = time.time()
        S._cycle_last_info = {
            "cycle": S.cycle_counter,
            "regime": S._latest_regime.get("regime", "?"),
            "cash": actual_idr_balance,
            "positions": len(S.positions),
            "duration": int(time.time() - _t0),
        }
        print(f"⏱ Cycle #{S.cycle_counter} finished in {int(time.time() - _t0)}s", flush=True)
        if S.cycle_counter % 100 == 0:
            from db import _checkpoint as _db_ck
            _db_ck()
            print(f"  DB checkpoint done (cycle #{S.cycle_counter})", flush=True)
