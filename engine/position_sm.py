# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""engine/position_sm.py — mesin status TP/SL/trailing per posisi terbuka.

Dipotong dari main.py (#8 tahap 2). `main.py` dulu punya 2.400 baris karena
state machine posisi — bagian yang paling sering berubah dan paling berbahaya
saat salah `await` — bercampur dengan orkestrasi proses. Di sini ia berdiri
sendiri: cuma bergantung pada state (`engine.state`), executor, dan notifier,
jadi bisa dites tanpa menyentuh entrypoint.

Kontrak: setiap fungsi di modul ini HANYA boleh dipanggil dari dalam
`S._state_lock` (jalur _state_consumer dan portfolio_cycle). Tidak ada `await`
di tengah baca-tulis `_position_states` tanpa lock itu.
"""

import time

import httpx

import config
from executor import place_order, cancel_order
from notifier import send_message
from engine import state as S


def pnl_pct(entry: float, current: float, side: str) -> float:
    if entry <= 0:
        return 0
    if side.upper() == "BUY":
        return (current - entry) / entry * 100
    return (entry - current) / entry * 100

def add_position(pos_list: list[dict], pair: str, side: str, entry_price: float, qty: float,
                  amount_idr: float, atr_pct: float | None, entry_time: float, entry_mode: str):
    existing = next((p for p in pos_list if p["pair"] == pair), None)
    if existing:
        total_qty = existing["qty"] + qty
        if total_qty > 0:
            existing["entry_price"] = (existing["qty"] * existing.get("entry_price", 0) + qty * entry_price) / total_qty
        existing["qty"] = total_qty
        existing["amount_idr"] += amount_idr
        if atr_pct is not None:
            existing["atr_pct"] = atr_pct
        if entry_mode:
            existing["entry_mode"] = entry_mode
    else:
        pos_list.append({
            "pair": pair, "side": side,
            "entry_price": entry_price, "qty": qty,
            "amount_idr": amount_idr,
            "atr_pct": atr_pct, "entry_time": entry_time,
            "entry_mode": entry_mode or "KONSERVATIF",
        })

async def _sm_cancel(client: httpx.AsyncClient, oid: int, pair: str) -> bool:
    try:
        d = await cancel_order(client, oid, pair=pair, side="sell")
        return d.get("success") == 1
    except Exception:
        return False

async def _sm_place_sell(client: httpx.AsyncClient, pair: str, qty: float, price: int) -> dict | None:
    try:
        return await place_order(client, "sell", price, qty * price, pair=pair,
                                 order_type="limit", qty=qty)
    except Exception:
        return None

async def _sm_place_tp(client: httpx.AsyncClient, pair: str, qty: float, entry: float, atr: float, mult: float | None = None) -> int | None:
    m = mult if mult is not None else config.ATR_TP_MULTIPLIER
    tp_price = int(entry * (1 + max(atr, 0.5) * m / 100))
    ret = await _sm_place_sell(client, pair, qty, tp_price)
    if ret and ret.get("order_id"):
        oid = int(ret["order_id"])
        S._position_states[pair]["tp_order_id"] = oid
        S._position_states[pair]["tp_price"] = tp_price
        S._position_states[pair]["state"] = "TP_ACTIVE"
        print(f"  SM TP PLACED: {pair} @ Rp{tp_price:,} (oid={oid})", flush=True)
        return oid
    return None

async def _sm_place_sl(client: httpx.AsyncClient, pair: str, qty: float, entry: float, atr: float, mult: float | None = None) -> int | None:
    m = mult if mult is not None else config.ATR_SL_MULTIPLIER
    sl_pct = max(atr * m / 100, 0.015)
    sl_price = int(entry * (1 - sl_pct))
    ret = await _sm_place_sell(client, pair, qty, sl_price)
    if ret and ret.get("order_id"):
        oid = int(ret["order_id"])
        S._position_states[pair]["sl_order_id"] = oid
        S._position_states[pair]["sl_price"] = sl_price
        return oid
    return None

def _sm_init(pair: str, entry: float, qty: float, atr: float, mode: str = "TP_ACTIVE"):
    S._position_states[pair] = {
        "state": "NEW",
        "tp_order_id": None,
        "sl_order_id": None,
        "tp_price": 0,
        "sl_price": 0,
        "entry_price": entry,
        "qty": qty,
        "atr_pct": atr,
        "target_mode": mode,
        "trailing_high": entry,
    }

def _sm_cleanup(pair: str):
    S._position_states.pop(pair, None)
    S.risk.trailing_highs.pop(pair, None)
    S.risk._initial_sl_released.pop(pair, None)
    S.risk._pyramid_done.pop(pair, None)
    S._ext_entry_prices.pop(pair, None)
    S._order_error_cooldown.pop(pair, None)
    S._realtime_sltp_last.pop(pair, None)
    S._pyramid_cooldown.pop(pair, None)
    S._momentum_entry_time.pop(pair, None)

def _sm_get(pair: str) -> dict | None:
    return S._position_states.get(pair)

async def _handle_fill_event(pair: str, side: str, status: str) -> None:
    sm = S._position_states.get(pair)
    if status in ("FILLED", "FILL", "DONE", "PARTIALLY_FILLED") and side == "sell":
        if sm:
            print(f"  PWS FILL TRIGGER: {pair} — SM aware", flush=True)
    elif status in ("CANCELLED", "REJECTED"):
        print(f"  PWS CANCELLED: {pair} — SM akan re-place di cycle berikutnya", flush=True)

async def _realtime_sltp_check(pair: str, price: float):
    pass  # state pindah ke engine/state.py
    now_t = time.time()
    if now_t - S._realtime_sltp_last.get(pair, 0) < 10:
        return
    S._realtime_sltp_last[pair] = now_t
    if S.shutdown_flag:
        return
    sm = S._position_states.get(pair)
    if not sm:
        return
    pos_check = next((x for x in S.positions if x["pair"] == pair), None)
    if not pos_check:
        _sm_cleanup(pair)
        return
    entry = sm["entry_price"]
    atr = sm.get("atr_pct", 1.0)
    if S._latest_ohlcv_map_1h.get(pair) and len(S._latest_ohlcv_map_1h[pair]) >= 20:
        recalc = S.risk.compute_atr(S._latest_ohlcv_map_1h[pair])
        if recalc > atr:
            atr = recalc
    sl_level = entry * (1 - max(atr, 0.5) * config.ATR_SL_MULTIPLIER / 100)
    recovery_level = entry * 1.005

    if sm["state"] == "TP_ACTIVE":
        if price >= entry * 1.025 and S._latest_regime.get("regime") == "BULL" and (sm.get("tp_price", 0) == 0 or price < sm["tp_price"] * 0.995):
            async with httpx.AsyncClient() as c:
                if sm.get("tp_order_id"):
                    await _sm_cancel(c, sm["tp_order_id"], pair)
                p = next((x for x in S.positions if x["pair"] == pair), None)
                if p:
                    trail_price_min = int(entry * 1.005)
                    oid = await _sm_place_sl(c, pair, p["qty"], trail_price_min if price < trail_price_min else price, atr, mult=config.ROTHSCHILD_TRAILING_SL_ATR)
                    if oid:
                        sm["sl_order_id"] = oid
                        sm["tp_order_id"] = None
                        sm["state"] = "TRAILING"
                        sm["trailing_high"] = price
                        await send_message(f"🚀 SM TRAILING ON: {pair}")
                        print(f"  SM → TRAILING: {pair} @ {price:,} ({(price/entry-1)*100:.1f}%)", flush=True)
        if price <= sl_level:
            async with httpx.AsyncClient() as c:
                if sm.get("tp_order_id"):
                    await _sm_cancel(c, sm["tp_order_id"], pair)
                p = next((x for x in S.positions if x["pair"] == pair), None)
                if p:
                    oid = await _sm_place_sl(c, pair, p["qty"], entry, atr)
                    if oid:
                        sm["sl_order_id"] = oid
                        sm["tp_order_id"] = None
                        sm["state"] = "SL_ACTIVE"
                        await send_message(f"⚡ SM SL: {pair} (cancel TP, place SL @ Rp{int(sl_level):,})")
                        print(f"  SM → SL: {pair} @ Rp{int(sl_level):,} (price {price:,.0f})", flush=True)

    elif sm["state"] == "SL_ACTIVE":
        if price >= recovery_level:
            async with httpx.AsyncClient() as c:
                if sm.get("sl_order_id"):
                    await _sm_cancel(c, sm["sl_order_id"], pair)
                p = next((x for x in S.positions if x["pair"] == pair), None)
                if p:
                    oid = await _sm_place_tp(c, pair, p["qty"], entry, atr)
                    if oid:
                        sm["tp_order_id"] = oid
                        sm["sl_order_id"] = None
                        sm["state"] = "TP_ACTIVE"
                        await send_message(f"⚡ SM TP: {pair} (cancel SL, place TP @ Rp{int(sl_level):,})")
                        print(f"  SM → TP: {pair} @ Rp{int(sl_level):,} (price {price:,.0f})", flush=True)

    elif sm["state"] == "TRAILING":
        pnl_pct = (price - entry) / entry * 100 if entry else 0
        if price > sm.get("trailing_high", entry) * 1.001:
            old_high = sm.get("trailing_high", entry)
            sm["trailing_high"] = price
            trail_price = int(price * (1 - max(atr, 0.5) * config.ROTHSCHILD_TRAILING_SL_ATR / 100))
            trail_price = max(trail_price, int(entry * 1.005))
            if sm.get("sl_order_id") and trail_price > sm.get("sl_price", 0):
                async with httpx.AsyncClient() as c:
                    await _sm_cancel(c, sm["sl_order_id"], pair)
                    p = next((x for x in S.positions if x["pair"] == pair), None)
                    if p:
                        oid = await _sm_place_sell(c, pair, p["qty"], trail_price)
                        if oid:
                            sm["sl_order_id"] = oid
                            sm["sl_price"] = trail_price
                            print(f"  SM TRAIL: {pair} trailing_high={price:,.0f} sl={trail_price:,} (oid={oid})", flush=True)
            if pnl_pct >= max(atr, 0.5) * config.ROTHSCHILD_PYRAMID_TRIGGER / 100:
                now_pyr = time.time()
                if now_pyr - S._pyramid_cooldown.get(pair, 0) < 300:
                    print(f"  SM PYRAMID SKIP: {pair} cooldown ({int(now_pyr - S._pyramid_cooldown.get(pair, 0))}s)", flush=True)
                    return
                pyr_high = sm.get("_pyramid_high", 0)
                if pyr_high > 0 and price <= pyr_high:
                    print(f"  SM PYRAMID SKIP: {pair} price {price:.0f} <= last pyramid {pyr_high:.0f} (trend turun)", flush=True)
                    return
                sm["_pyramid_high"] = price
                pyr_pos = next((x for x in S.positions if x["pair"] == pair), None)
                if not pyr_pos:
                    _sm_cleanup(pair)
                    return
                if not S._daily_loss_hit_today or S._greed_used_today:
                    pyr_amt = int(max(config.MIN_ORDER_IDR, S._latest_balance * config.ROTHSCHILD_PYRAMID_MULT))
                    if pyr_amt >= config.MIN_ORDER_IDR and pyr_amt <= S._latest_balance:
                        async with httpx.AsyncClient() as _pc:
                            try:
                                pyr_order = await place_order(_pc, "buy", price, pyr_amt, pair=pair, order_type="market")
                                if pyr_order.get("order_id") or pyr_order.get("receive_rp"):
                                    coin_n = pair.split("_")[0]
                                    pyr_f = float(pyr_order.get(f"receive_{coin_n}", 0)) or (pyr_amt / price)
                                    pyr_spend = float(pyr_order.get("spend_rp", 0)) or pyr_amt
                                    S._latest_balance -= pyr_spend
                                    old_qty = sm["qty"]
                                    old_entry = sm["entry_price"]
                                    total_qty = old_qty + pyr_f
                                    new_entry = (old_entry * old_qty + price * pyr_f) / total_qty if total_qty > 0 else price
                                    sm["entry_price"] = new_entry
                                    sm["qty"] = total_qty
                                    add_position(S.positions, pair, "BUY", price, pyr_f, pyr_spend, atr, time.time(), "TRAILING")
                                    S._pyramid_cooldown[pair] = now_pyr
                                    if sm.get("sl_order_id"):
                                        await cancel_order(_pc, sm["sl_order_id"], pair=pair, side="sell")
                                        sm["sl_order_id"] = None
                                    p_sm = next((x for x in S.positions if x["pair"] == pair), None)
                                    if p_sm:
                                        new_oid = await _sm_place_sl(_pc, pair, p_sm["qty"], new_entry, atr, mult=config.ROTHSCHILD_TRAILING_SL_ATR)
                                        if new_oid:
                                            sm["sl_order_id"] = new_oid
                                    print(f"  SM PYRAMID: {pair} +{pyr_f:.6f} @ {price:,.0f} avg_entry={new_entry:,.0f}", flush=True)
                                    await send_message(f"🔺 SM PYRAMID: {pair} +{pyr_f:.6f} @ Rp{price:,.0f}")
                            except Exception as e:
                                print(f"  SM pyramid fail {pair}: {e}", flush=True)
