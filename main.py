# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import asyncio
import datetime
import hashlib
import hmac
import math
import os
import sys
import signal
import time
from urllib.parse import urlencode
import httpx
import config
from data_layer import fetch_viable_pairs, fetch_ticker, fetch_ohlcv, fetch_ohlcv_both, fetch_all_tickers, fetch_orderbook
from indicators import compute_signals, compute_batch_signals
from hmm_regime import HMMRegimeDetector
from risk_manager import RiskManager, PortfolioRiskManager
from executor import place_order, get_balance, get_order
from deadman import refresh_deadman, cancel_deadman
from notifier import send_message
from db import init_db, log_trade, log_decision, get_recent_trades, get_trade_count_today, get_trades_by_period, get_recent_completed_sells, count_new_completed_sells, get_max_trade_id, init_chat_db
import persist
from market_ws import market_ws_loop, LIVE_TICKERS, stop as mws_stop, set_on_tick
from private_ws import private_ws_loop, stop as pws_stop
from momentum import MomentumEngine
import rules
import patterns
import pairs
from optimizer import AIOptimizer

risk = RiskManager()
portfolio_risk = PortfolioRiskManager()
hmm_detector = HMMRegimeDetector(n_states=config.HMM_N_STATES)
positions: list[dict] = []
shutdown_flag = False
momentum_engine = MomentumEngine()
optimizer = AIOptimizer()

regime_history: list[str] = []
known_pairs: set[str] = set()
_ext_entry_prices: dict[str, float] = {}
_pair_meta: dict[str, dict] = {}
_hmm_trained_cycle = 0
_prev_regime: str = ""
_prev_equity: float = 0
_report_sent_count: int = 0
_coin_blacklist: set[str] = set()
_cio_stats: dict = {"total_decisions": 0, "buys": 0, "sells": 0, "wins": 0, "losses": 0}
_tp_limit_orders: dict[str, int] = {}
_pending_orders: dict[str, dict] = {}
_pending_sells: dict[str, dict] = {}
_cooldown: dict[str, float] = {}
_latest_regime: dict = {}
_latest_ticker_map: dict = {}
_latest_all_signals: dict = {}
_latest_ohlcv_map_1h: dict = {}
_order_error_cooldown: dict[str, float] = {}
_realtime_sltp_last: dict[str, float] = {}
_realtime_sold: set[str] = set()
_realtime_sold_time: dict[str, float] = {}
_position_states: dict[str, dict] = {}
_sm_cooldown: dict[str, float] = {}
_momentum_entry_time: dict[str, float] = {}
_pyramid_cooldown: dict[str, float] = {}
_cycle_last_end: float = 0
_cycle_last_info: dict = {}
_recent_actions: list[dict] = []
_realized_pnl_idr: float = 0.0
_rothschild_active: bool = False
_regime_bull_streak: int = 0
_regime_bear_streak: int = 0
_daily_loss_hit_today: bool = False
_greed_used_today: bool = False
_cb_consecutive_loss_days: int = 0
_cb_last_loss_date: str = ""
_cb_triggered_at: float = 0
_cb_active_until: float = 0

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
        hmm_result = hmm_detector.predict(ohlcv_map_1h) if ohlcv_map_1h else {}
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

def handle_sig(*_):
    global shutdown_flag
    shutdown_flag = True

def fmt_qty(pair: str, qty: float) -> str:
    meta = _pair_meta.get(pair, {})
    vp = meta.get("vol_precision", 0)
    if vp == 0:
        return str(int(qty))
    min_traded = meta.get("min_traded", 0.0001)
    if min_traded >= 1:
        return str(int(qty))
    if min_traded >= 0.01:
        return f"{qty:.2f}"
    if min_traded >= 0.001:
        return f"{qty:.4f}"
    if abs(qty - round(qty)) < 1e-8:
        return str(int(qty))
    s = f"{qty:.8f}".rstrip("0").rstrip(".")
    return s if s != "0" else "0"

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
        cb = urlencode({"method":"cancelOrder","timestamp":int(time.time()*1000),"recvWindow":"5000","pair":pair,"order_id":str(oid),"type":"sell"})
        cs = hmac.new(config.INDODAX_SECRET_KEY.encode(), cb.encode(), hashlib.sha512).hexdigest()
        r = await client.post(config.INDODAX_TAPI_URL, headers={"Key":config.INDODAX_API_KEY,"Sign":cs,"Content-Type":"application/x-www-form-urlencoded"}, content=cb)
        return r.json().get("success") == 1
    except Exception:
        return False

async def _sm_place_sell(client: httpx.AsyncClient, pair: str, qty: float, price: int) -> dict | None:
    coin = pair.split("_")[0]
    qs = f"{qty:.8f}".rstrip("0").rstrip(".") or "0"
    sp = {"method":"trade","timestamp":int(time.time()*1000),"recvWindow":"5000","pair":pair,"type":"sell",coin: qs,"price":str(price),"order_type":"limit"}
    sb = urlencode(sp)
    ss = hmac.new(config.INDODAX_SECRET_KEY.encode(), sb.encode(), hashlib.sha512).hexdigest()
    try:
        r = await client.post(config.INDODAX_TAPI_URL, headers={"Key":config.INDODAX_API_KEY,"Sign":ss,"Content-Type":"application/x-www-form-urlencoded"}, content=sb)
        d = r.json()
        if d.get("success") == 1:
            return d["return"]
    except Exception:
        pass
    return None

async def _sm_place_tp(client: httpx.AsyncClient, pair: str, qty: float, entry: float, atr: float, mult: float | None = None) -> int | None:
    m = mult if mult is not None else config.ATR_TP_MULTIPLIER
    tp_price = int(entry * (1 + max(atr, 0.5) * m / 100))
    ret = await _sm_place_sell(client, pair, qty, tp_price)
    if ret and ret.get("order_id"):
        oid = int(ret["order_id"])
        _position_states[pair]["tp_order_id"] = oid
        _position_states[pair]["tp_price"] = tp_price
        _position_states[pair]["state"] = "TP_ACTIVE"
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
        _position_states[pair]["sl_order_id"] = oid
        _position_states[pair]["sl_price"] = sl_price
        return oid
    return None

def _sm_init(pair: str, entry: float, qty: float, atr: float, mode: str = "TP_ACTIVE"):
    _position_states[pair] = {
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
    _position_states.pop(pair, None)

def _sm_get(pair: str) -> dict | None:
    return _position_states.get(pair)

cycle_counter = 0

async def portfolio_cycle(client: httpx.AsyncClient):
    global positions, cycle_counter, _prev_regime, _prev_equity, _report_sent_count, _latest_regime, _latest_ticker_map, _latest_all_signals, _latest_ohlcv_map_1h, _latest_balance, _realized_pnl_idr, _realtime_sold, _realtime_sold_time
    global _daily_loss_hit_today, _greed_used_today, _rothschild_active, _regime_bull_streak, _regime_bear_streak
    global _cb_consecutive_loss_days, _cb_last_loss_date, _cb_triggered_at, _cb_active_until, shutdown_flag, _pending_sells
    cycle_counter += 1
    _t0 = time.time()
    risk.daily_loss_stopped = False
    actual_idr_balance = 0

    if _cb_active_until > time.time():
        print(f"🛑 CIRCUIT BREAKER ACTIVE — cooling down until {datetime.datetime.fromtimestamp(_cb_active_until).strftime('%Y-%m-%d %H:%M')} WIB", flush=True)
        return
    if _cb_active_until > 0 and _cb_active_until <= time.time():
        _cb_consecutive_loss_days = 0
        _cb_active_until = 0
        _cb_triggered_at = 0
        await send_message("✅ CIRCUIT BREAKER selesai — bot lanjut normal")
        persist.save_circuit_breaker({"consecutive_loss_days": 0, "last_loss_date": "", "triggered_at": 0, "active_until": 0})

    for pid in list(_pending_orders.keys()):
        try:
            po = _pending_orders[pid]
            oid = po["order_id"]
            order_info = await get_order(client, oid, pair=pid)
            if not order_info:
                del _pending_orders[pid]
                continue
            status = order_info.get("status", "").lower()
            remain = float(order_info.get("remain_rp", order_info.get("remain_idr", 0)) or 0)
            is_filled = status in ("filled",) or remain == 0
            if is_filled and po.get("is_maker"):
                qty_filled = float(order_info.get(f"receive_{po.get('side', 'buy').lower()}", po["qty"]))
                add_position(positions, pid, po.get("side", "BUY"), po["price"], po["qty"],
                             po["amount_idr"], po.get("atr_pct"), time.time(),
                             "ROTHSCHILD" if _rothschild_active else "KONSERVATIF")
                persist.save_positions(positions)
                print(f"  MAKER FILLED: {pid} → position opened", flush=True)
                await send_message(f"✅ MAKER FILLED: BUY {pid}\nRp{po['amount_idr']:,.0f} @ {po['price']:,.0f}")
                del _pending_orders[pid]
                continue
            if status in ("cancelled", "rejected") or remain == 0:
                del _pending_orders[pid]
                continue
            if po.get("is_maker"):
                po["cycles"] = po.get("cycles", 0) + 1
                grace = max(1, int(config.LIMIT_GRACE_CYCLE))
                if po["cycles"] >= grace:
                    try:
                        cancel_body = urlencode({
                            "method": "cancelOrder", "timestamp": int(time.time() * 1000),
                            "recvWindow": "5000", "pair": pid,
                            "order_id": str(oid), "type": po.get("side", "buy").lower(),
                        })
                        cancel_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body.encode(), hashlib.sha512).hexdigest()
                        await client.post(config.INDODAX_TAPI_URL, headers={
                            "Key": config.INDODAX_API_KEY, "Sign": cancel_sig,
                            "Content-Type": "application/x-www-form-urlencoded",
                        }, content=cancel_body)
                        print(f"  LIMIT UNFILLED: {pid} order_id={oid} — cancel, cari koin lain", flush=True)
                    except Exception as e:
                        print(f"  Cancel unfilled order failed {pid}: {e}", flush=True)
                    del _pending_orders[pid]
        except Exception:
            po = _pending_orders.get(pid)
            if po and po.get("order_id"):
                try:
                    cancel_body_b = urlencode({
                        "method": "cancelOrder", "timestamp": int(time.time() * 1000),
                        "recvWindow": "5000", "pair": pid,
                        "order_id": str(po["order_id"]), "type": po.get("side", "buy").lower(),
                    })
                    cancel_sig_b = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body_b.encode(), hashlib.sha512).hexdigest()
                    await client.post(config.INDODAX_TAPI_URL, headers={
                        "Key": config.INDODAX_API_KEY, "Sign": cancel_sig_b,
                        "Content-Type": "application/x-www-form-urlencoded",
                    }, content=cancel_body_b)
                    print(f"  CANCEL STUCK: {pid} order_id={po['order_id']}", flush=True)
                except Exception:
                    pass
            del _pending_orders[pid]

    for pid in list(_pending_sells.keys()):
        try:
            ps = _pending_sells[pid]
            oid = ps["order_id"]
            order_info = await get_order(client, oid, pair=pid)
            if not order_info:
                del _pending_sells[pid]
                continue
            status = order_info.get("status", "").lower()
            remain = float(order_info.get(f"remain_{pid.split('_')[0]}", 0))
            is_filled = status in ("filled",) or remain <= 0
            if is_filled:
                print(f"  PENDING SELL FILLED: {pid}", flush=True)
                p = next((x for x in positions if x["pair"] == pid), None)
                if p:
                    pnl = (float(order_info.get("price", 0)) - p.get("entry_price", 0)) * p["qty"]
                    positions.remove(p)
                    persist.save_positions(positions)
                    log_trade("sell", float(order_info.get("price", 0)), ps["qty"], ps["amount"], status="closed", pnl=pnl, reason=f"pending_sell_filled {pid}")
                del _pending_sells[pid]
                continue
            if status in ("cancelled", "rejected"):
                p = next((x for x in positions if x["pair"] == pid), None)
                if p and pid not in [ps2["pair"] for ps2 in _pending_sells.values()]:
                    pass
                del _pending_sells[pid]
                continue
            ps["cycles"] = ps.get("cycles", 0) + 1
            if ps["cycles"] >= config.SELL_GRACE_CYCLE:
                try:
                    cancel_body_s = urlencode({"method":"cancelOrder","timestamp":int(time.time()*1000),"recvWindow":"5000","pair":pid,"order_id":str(oid),"type":"sell"})
                    cancel_sig_s = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body_s.encode(), hashlib.sha512).hexdigest()
                    await client.post(config.INDODAX_TAPI_URL, headers={"Key":config.INDODAX_API_KEY,"Sign":cancel_sig_s,"Content-Type":"application/x-www-form-urlencoded"}, content=cancel_body_s)
                except Exception:
                    pass
                bid = int(_latest_ticker_map.get(pid, {}).get("buy", 0))
                if bid > 20:
                    coin = pid.split("_")[0]
                    qty_s = f"{ps['qty']:.8f}".rstrip("0").rstrip(".") or "0"
                    sp_s = {"method":"trade","timestamp":int(time.time()*1000),"recvWindow":"5000","pair":pid,"type":"sell",coin: qty_s,"price":str(bid),"order_type":"limit"}
                    sb_s = urlencode(sp_s)
                    ss_s = hmac.new(config.INDODAX_SECRET_KEY.encode(), sb_s.encode(), hashlib.sha512).hexdigest()
                    sr_s = await client.post(config.INDODAX_TAPI_URL, headers={"Key":config.INDODAX_API_KEY,"Sign":ss_s,"Content-Type":"application/x-www-form-urlencoded"}, content=sb_s)
                    sj_s = sr_s.json()
                    if sj_s.get("success") == 1:
                        print(f"  SELL RETRY AT BID: {pid} @ {bid}", flush=True)
                    else:
                        print(f"  SELL RETRY FAILED: {pid} — {sj_s.get('error','?')}", flush=True)
                del _pending_sells[pid]
        except Exception:
            if pid in _pending_sells:
                del _pending_sells[pid]

    for pid, sm in list(_position_states.items()):
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
                p = next((x for x in positions if x["pair"] == pid), None)
                if p:
                    positions.remove(p)
                    persist.save_positions(positions)
                    log_trade("sell", fill_price, sm["qty"], fill_price * sm["qty"], status="closed", pnl=pnl, reason=f"sm_{sm['state']} {pid}")
                    if config.AUTO_COMPOUND:
                        _realized_pnl_idr += pnl
                    label = "TP" if sm["state"] == "TP_ACTIVE" else "SL"
                    emoji = "🟢" if pnl >= 0 else "🔴"
                    await send_message(f"{emoji} SM {label}: {pid}\n{pnl:+.0f} IDR @ Rp{fill_price:,}")
                    now_rs = time.time()
                    _realtime_sold_time[pid] = now_rs
                    _realtime_sold.add(pid)
                    stale_rs = {p for p in _realtime_sold if now_rs - _realtime_sold_time.get(p, 0) > 300}
                    _realtime_sold -= stale_rs
                    print(f"  SM FILLED: {pid} {sm['state']} @ Rp{fill_price:,} ({pnl:+.0f} IDR)", flush=True)
                    if pnl < 0:
                        _coin_blacklist.add(pid)
                    cd_time = 21600 if pnl >= 0 else 86400
                    _sm_cooldown[pid] = time.time() + cd_time
                    persist.save_sm_cooldown(_sm_cooldown)
                    _sm_cleanup(pid)
                continue
            if oi is None:
                print(f"  SM ORDER UNKNOWN: {pid} {sm['state']} oid={oid} — skip (network error)", flush=True)
                continue
            status = oi.get("status", "").lower()
            if status in ("cancelled", "rejected"):
                print(f"  SM ORDER CANCELLED: {pid} {sm['state']} — re-place", flush=True)
                p = next((x for x in positions if x["pair"] == pid), None)
                if p:
                    atr = p.get("atr_pct") or risk.compute_atr(_latest_ohlcv_map_1h.get(pid, []))
                    sm["tp_order_id"] = None
                    sm["sl_order_id"] = None
                    if sm["state"] in ("TP_ACTIVE", "SL_ACTIVE"):
                        oid = await _sm_place_tp(client, pid, p["qty"], sm["entry_price"], atr)
                        if oid:
                            print(f"  SM TP RE-PLACED: {pid} oid={oid}", flush=True)
                        else:
                            print(f"  SM TP RE-PLACE FAILED: {pid} — PENDING, retry next cycle", flush=True)
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
                lp = LIVE_TICKERS.get(pid, {}).get("last") or _latest_ticker_map.get(pid, {}).get("last", 0)
                if lp > 0 and lp < sm["sl_price"] * 0.97:
                    print(f"  SM HARD SL: {pid} price Rp{lp:,.0f} < 97% of SL Rp{sm['sl_price']:,} — force market sell", flush=True)
                    p = next((x for x in positions if x["pair"] == pid), None)
                    if p:
                        try:
                            await _sm_cancel(client, oid, pid)
                            coin_name = pid.split("_")[0]
                            qty_s = f"{p['qty']:.8f}".rstrip("0").rstrip(".") or "0"
                            bid = int(_latest_ticker_map.get(pid, {}).get("buy", lp))
                            sp_h = {"method":"trade","timestamp":int(time.time()*1000),"recvWindow":"5000","pair":pid,"type":"sell",coin_name: qty_s,"price":str(bid),"order_type":"limit"}
                            sb_h = urlencode(sp_h)
                            ss_h = hmac.new(config.INDODAX_SECRET_KEY.encode(), sb_h.encode(), hashlib.sha512).hexdigest()
                            sr_h = await client.post(config.INDODAX_TAPI_URL, headers={"Key":config.INDODAX_API_KEY,"Sign":ss_h,"Content-Type":"application/x-www-form-urlencoded"}, content=sb_h)
                            sj_h = sr_h.json()
                            if sj_h.get("success") == 1:
                                fill_p = float(sj_h["return"].get("price", bid))
                                pnl_h = (fill_p - sm["entry_price"]) * sm["qty"]
                                positions.remove(p)
                                persist.save_positions(positions)
                                log_trade("sell", fill_p, sm["qty"], fill_p * sm["qty"], status="closed", pnl=pnl_h, reason=f"sm_hard_sl {pid}")
                                if config.AUTO_COMPOUND:
                                    _realized_pnl_idr += pnl_h
                                await send_message(f"🔴 SM HARD SL: {pid}\n{pnl_h:+.0f} IDR @ Rp{fill_p:,}")
                                _coin_blacklist.add(pid)
                                cd_time = 86400
                                _sm_cooldown[pid] = time.time() + cd_time
                                persist.save_sm_cooldown(_sm_cooldown)
                                _sm_cleanup(pid)
                                print(f"  SM HARD SL EXECUTED: {pid} @ Rp{fill_p:,} ({pnl_h:+.0f} IDR)", flush=True)
                        except Exception as e:
                            print(f"  SM HARD SL failed {pid}: {e}", flush=True)
        except Exception:
            pass

    try:
        print("Scanning market for viable pairs...", flush=True)
        viable = await fetch_viable_pairs(client)
        print(f"Found {len(viable)} viable IDR pairs", flush=True)
        current_pairs = {v["pair"] for v in viable}
        new_coins = current_pairs - known_pairs
        if new_coins:
            print(f"New coins detected: {', '.join(new_coins)}", flush=True)
        known_pairs.update(current_pairs)
        for v in viable:
            _pair_meta[v["pair"]] = {
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

        for p in list(positions):
            pm = _pair_meta.get(p["pair"])
            if pm:
                val = p["qty"] * (ticker_map.get(p["pair"], {}).get("last") or LIVE_TICKERS.get(p["pair"], {}).get("last") or p["entry_price"])
                if val < pm["min_base"]:
                    pid = p["pair"]
                    sm = _position_states.get(pid)
                    if sm:
                        oid = sm.get("tp_order_id") or sm.get("sl_order_id")
                        if oid:
                            try:
                                await _sm_cancel(client, oid, pid)
                            except Exception:
                                pass
                        _sm_cleanup(pid)
                    if pid in _pending_sells:
                        try:
                            await _sm_cancel(client, _pending_sells[pid]["order_id"], pid)
                        except Exception:
                            pass
                        del _pending_sells[pid]
                    print(f"  CLEANUP: {pid} dust Rp{val:,.0f} < min Rp{pm['min_base']:,} — hapus", flush=True)
                    await send_message(f"🧹 Dust {pid} Rp{int(val):,} dihapus (di bawah min order)")
                    positions.remove(p)
                    persist.save_positions(positions)

        print(f"Computing signals: {len(ohlcv_map_1h)} pairs (1h) + {len(ohlcv_map_4h)} (4h)...", flush=True)
        all_signals = compute_batch_signals(ohlcv_map_1h, ohlcv_map_4h)

        regime_info = classify_regime(all_signals, ohlcv_map_1h)
        regime_history.append(regime_info["regime"])
        if len(regime_history) > 12:
            regime_history.pop(0)
        hmm_tag = f" HMM:{regime_info.get('hmm_regime', '?')}({regime_info.get('hmm_confidence', 0):.2f})" if regime_info.get('hmm_confidence', 0) > 0 else ""
        _now_t = time.time()
        if regime_info["regime"] != _prev_regime and _prev_regime and _now_t - getattr(portfolio_cycle, "_last_regime_notify", 0) > 1800:
            portfolio_cycle._last_regime_notify = _now_t
            await send_message(f"🔄 Regime: {_prev_regime} → {regime_info['regime']}{hmm_tag}")
        print(f"Regime: {regime_info['regime']}{hmm_tag} | B:{regime_info['buy_ratio']} S:{regime_info['sell_ratio']} "
              f"Score:{regime_info['avg_score']} HC:{regime_info['high_conviction_count']}", flush=True)

        current_regime = regime_info["regime"]

        if current_regime == "BULL":
            _regime_bull_streak += 1
            _regime_bear_streak = 0
        elif current_regime == "BEAR":
            _regime_bear_streak += 1
            _regime_bull_streak = 0
        elif current_regime in ("SIDEWAYS", "SIDEWAYS_LOW_VOL"):
            _regime_bull_streak = 0
            _regime_bear_streak = 0
        else:
            pass

        if _regime_bull_streak >= config.REGIME_STABILITY_CYCLES and not _rothschild_active:
            _rothschild_active = True
            config.ROTHSCHILD_ACTIVE = True
            config.MAX_OPEN_POSITIONS = config.ROTHSCHILD_OPEN_POSITIONS
            config.MAX_POSITION_PCT_PER_ASSET = config.ROTHSCHILD_POSITION_PCT
            print(f"  🚀 ROTHSCHILD AKTIF — {config.ROTHSCHILD_OPEN_POSITIONS} slot @{config.ROTHSCHILD_POSITION_PCT*100:.0f}%", flush=True)
            await send_message(f"🚀 ROTHSCHILD MODE — {config.ROTHSCHILD_OPEN_POSITIONS} slot, pyramid ON")
        elif current_regime != "BULL" and _rothschild_active and (_regime_bear_streak >= config.REGIME_STABILITY_BEAR_CYCLES or current_regime == "SIDEWAYS"):
            _rothschild_active = False
            config.ROTHSCHILD_ACTIVE = False
            config.MAX_OPEN_POSITIONS = 4
            config.MAX_POSITION_PCT_PER_ASSET = 0.25
            print(f"  🛑 ROTHSCHILD OFF — {current_regime} detected, balik ke konservatif 4 slot @25%", flush=True)
            await send_message(f"🛑 ROTHSCHILD OFF — {current_regime} mode, konservatif 4 slot")
        mode_tag = "🔴 R" if _rothschild_active else "🟢 K"
        kelly_alloc = portfolio_risk.kelly_for_regime(current_regime)
        if not _rothschild_active:
            config.MAX_POSITION_PCT_PER_ASSET = kelly_alloc
        print(f"  Mode: {mode_tag} (regime: {current_regime}, kelly: {kelly_alloc:.0%}, bull streak: {_regime_bull_streak}, bear: {_regime_bear_streak})", flush=True)

        actual_idr_balance = 0

        if config.INDODAX_API_KEY and config.INDODAX_SECRET_KEY:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                hold = info.get("balance_hold", {})
                actual_idr_balance = float(bal.get("idr", 0))
                for coin in set(list(bal.keys()) + list(hold.keys())):
                    raw_qty = bal.get(coin, 0)
                    qty = float(raw_qty) + float(hold.get(coin, 0))
                    if qty <= 0 or coin == "idr":
                        continue
                    pair = f"{coin}_idr"
                    if pair in config.STABLECOINS or pair in config.SKIP_COINS:
                        continue
                    last_price = ticker_map.get(pair, {}).get("last") or LIVE_TICKERS.get(pair, {}).get("last") or 0

                    old = next((p for p in positions if p["pair"] == pair), None)
                    if old:
                        if last_price > 0:
                            coin_value = qty * last_price
                            pair_min = _pair_meta.get(pair, {}).get("min_base", 10000)
                            if coin_value < pair_min and pair_min > 0:
                                if _position_states.get(pair):
                                    oid = _position_states[pair].get("tp_order_id") or _position_states[pair].get("sl_order_id")
                                    if oid:
                                        await _sm_cancel(client, oid, pair)
                                    _sm_cleanup(pair)
                                print(f"  {pair}: dust Rp{coin_value:,.0f} < min Rp{pair_min:,} — hapus", flush=True)
                                await send_message(f"🧹 Dust {pair} Rp{int(coin_value):,} dihapus (di bawah min order)")
                                positions.remove(old)
                                persist.save_positions(positions)
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
                        pm = _pair_meta.get(pair, {})
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

                    _saved = _ext_entry_prices.get(pair, 0)
                    entry_price = _saved if _saved > 0 and last_price > 0 and abs(_saved - last_price) / last_price < 0.2 else (last_price if last_price else _saved)
                    if entry_price > 0:
                        _ext_entry_prices[pair] = entry_price
                        persist.save_entry_prices(_ext_entry_prices)
                    if entry_price > 0:
                        print(f"  {pair}: entry_price={entry_price:,}", flush=True)

                    if pair in _realtime_sold:
                        print(f"  {pair}: skip restore (baru di-SM FILLED)", flush=True)
                        continue
                    positions.append({
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
                for p in list(positions):
                    if p["pair"] not in bal_coins:
                        print(f"  CLEANUP: {p['pair']} gak ada di balance — hapus", flush=True)
                        positions.remove(p)
                        persist.save_positions(positions)
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
                        for pid_sm, sm in list(_position_states.items()):
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
        paper_prices = await asyncio.gather(*[_coin_price(p["pair"]) for p in positions])
        paper_equity = 0
        for p, price in zip(positions, paper_prices):
            if price <= 0 and p.get("entry_price"):
                price = p["entry_price"]
            paper_equity += p["qty"] * price
        total_equity = actual_idr_balance + paper_equity
        dust_eq = getattr(portfolio_cycle, "_dust_equity", 0)
        if dust_eq:
            total_equity += dust_eq
            portfolio_cycle._dust_equity = 0
        if not positions and paper_equity == 0 and total_equity < config.MIN_ORDER_IDR:
            pass
        elif _prev_equity > 0 and total_equity < _prev_equity * 0.5 and paper_equity > config.MIN_ORDER_IDR:
            guard_count = getattr(portfolio_cycle, "_equity_guard", 0) + 1
            portfolio_cycle._equity_guard = guard_count
            if guard_count >= 5:
                print(f"  Equity drop confirmed ({guard_count}x) — accept Rp{total_equity:,.0f}", flush=True)
            else:
                print(f"  Equity suspicious ({guard_count}/5): Rp{total_equity:,.0f} vs prev Rp{_prev_equity:,.0f} — hold", flush=True)
                total_equity = _prev_equity
        else:
            portfolio_cycle._equity_guard = 0
        max_positions = config.ROTHSCHILD_OPEN_POSITIONS if _rothschild_active else config.MAX_OPEN_POSITIONS
        if cycle_counter == 1:
            portfolio_risk.peak_capital = total_equity
            persist.save_peak_capital(total_equity)
            print(f"  Initial equity: Rp{total_equity:,.0f}", flush=True)
        saved_peak = persist.load_peak_capital()
        if saved_peak and saved_peak > portfolio_risk.peak_capital:
            portfolio_risk.peak_capital = saved_peak
        if total_equity > portfolio_risk.peak_capital:
            portfolio_risk.peak_capital = total_equity
            persist.save_peak_capital(total_equity)

        base_eq = persist.load_initial_equity() or total_equity
        eq_pct = (total_equity - base_eq) / base_eq * 100 if base_eq else 0
        tag = "🔴 R" if _rothschild_active else "🟢 K"
        hold_tag = " ⏸️" if _daily_loss_hit_today else ""
        await send_message(
            f"💳 Rp{total_equity:,.0f} ({eq_pct:+.1f}%) {tag}{hold_tag}\n"
            f"Cycle #{cycle_counter} | {regime_info['regime']} | {len(positions)} pos | Cash: Rp{actual_idr_balance:,.0f}"
        )

        saved_peak = persist.load_today_peak()
        if saved_peak > risk.today_peak and 1_000 < saved_peak < 100_000_000_000:
            risk.today_peak = saved_peak
            print(f"  Restored today_peak: Rp{risk.today_peak:,.0f}", flush=True)

        if risk.today_peak > total_equity * 2:
            risk.today_peak = total_equity
            persist.save_today_peak(total_equity)
            print(f"  Today peak reset — WD/deposit detected: Rp{total_equity:,.0f}", flush=True)

        daily_limit = risk.check_daily_limits(total_equity)
        persist.save_today_peak(risk.today_peak)

        _today_d = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
        if persist.load_loss_hit_date() != _today_d:
            _daily_loss_hit_today = False
            _greed_used_today = False
            persist.save_daily_loss_hit(False)
            persist.save_loss_hit_date(_today_d)
            print(f"  Daily loss reset — new trading day", flush=True)
        if total_equity < config.EQUITY_FLOOR_IDR:
            print(f"🏴 EQUITY FLOOR: Rp{total_equity:,.0f} < Rp{config.EQUITY_FLOOR_IDR:,} — no cash, waiting", flush=True)
            return

        if daily_limit == "DAILY_LOSS_LIMIT":
            actual_loss = risk.today_peak - total_equity
            if _greed_used_today and actual_loss < config.DAILY_LOSS_FLOOR_IDR * 2:
                print(f"DAILY LOSS {actual_loss:,.0f} ≥ 15k TAPI GREED — lanjut (max Rp30k)", flush=True)
            else:
                _daily_loss_hit_today = True
                persist.save_daily_loss_hit(True)
                persist.save_loss_hit_date(_today_d)
                if _cb_last_loss_date != _today_d:
                    _cb_consecutive_loss_days += 1
                    _cb_last_loss_date = _today_d
                    print(f"CIRCUIT BREAKER: consecutive loss day #{_cb_consecutive_loss_days}", flush=True)
                    persist.save_circuit_breaker({"consecutive_loss_days": _cb_consecutive_loss_days, "last_loss_date": _cb_last_loss_date, "triggered_at": _cb_triggered_at, "active_until": _cb_active_until})
                    if _cb_consecutive_loss_days >= config.CIRCUIT_BREAKER_LIMIT:
                        _cb_triggered_at = time.time()
                        _cb_active_until = _cb_triggered_at + config.CIRCUIT_BREAKER_HOURS * 3600
                        persist.save_circuit_breaker({"consecutive_loss_days": _cb_consecutive_loss_days, "last_loss_date": _cb_last_loss_date, "triggered_at": _cb_triggered_at, "active_until": _cb_active_until})
                        cooldown_until = datetime.datetime.fromtimestamp(_cb_active_until).strftime("%Y-%m-%d %H:%M")
                        await send_message(f"🛑 CIRCUIT BREAKER: {_cb_consecutive_loss_days} hari loss berturut — bot stop sampai {cooldown_until} WIB")
                        print(f"🛑 CIRCUIT BREAKER: {_cb_consecutive_loss_days} consecutive loss days — stop until {cooldown_until}", flush=True)
                print(f"DAILY LOSS LIMIT HIT. Equity: Rp{total_equity:,.0f}. Realtime: TP izin, SL skip.", flush=True)
                return
        else:
            if _cb_last_loss_date != _today_d:
                _cb_consecutive_loss_days = 0
                _cb_last_loss_date = _today_d
                persist.save_circuit_breaker({"consecutive_loss_days": 0, "last_loss_date": "", "triggered_at": _cb_triggered_at, "active_until": _cb_active_until})

        if portfolio_risk.check_portfolio_stop(total_equity):
            actual_dd = (portfolio_risk.peak_capital - total_equity) / portfolio_risk.peak_capital * 100
            print(f"DRAWDOWN {actual_dd:.0f}% > {abs(config.PORTFOLIO_STOP_LOSS_PCT)*100:.0f}% — "
                  f"Equity: Rp{total_equity:,.0f}", flush=True)

        for p in positions:
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
            for p in positions
        ]

        if len(ohlcv_map_1h) >= 5 and (not hmm_detector.trained or cycle_counter % config.HMM_RETRAIN_INTERVAL == 0):
            try:
                hmm_detector.fit(ohlcv_map_1h)
                if hmm_detector.trained:
                    print(f"HMM regime detector trained ({config.HMM_N_STATES} states)", flush=True)
                    if cycle_counter <= 2:
                        await send_message(f"🧠 HMM: {regime_info.get('regime','?')} (confidence {regime_info.get('hmm_confidence',0):.0%})")
            except Exception as e:
                print(f"HMM train error: {e}", flush=True)
                hmm_detector.trained = False

        _prev_equity = total_equity
        _prev_regime = regime_info["regime"]

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
            all_signals, ticker_map, LIVE_TICKERS, positions,
            actual_idr_balance, total_equity, regime_info, ohlcv_map_1h,
            _coin_blacklist, _pair_meta, book_pressure_map=book_pressure_map,
            sm_cooldown=_sm_cooldown,
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

        for p in list(positions):
            pid = p["pair"]
            sm = _position_states.get(pid)
            if sm:
                if sm["state"] == "PENDING":
                    atr = p.get("atr_pct") or risk.compute_atr(ohlcv_map_1h.get(pid, []))
                    oid = await _sm_place_tp(client, pid, p["qty"], p["entry_price"], atr)
                    if oid:
                        print(f"  SM RETRY OK: {pid} tp_oid={oid}", flush=True)
                continue
            if pid in _sm_cooldown and _sm_cooldown[pid] > time.time():
                continue
            elif pid in _sm_cooldown:
                del _sm_cooldown[pid]
            atr = p.get("atr_pct") or risk.compute_atr(ohlcv_map_1h.get(pid, []))
            if atr and p["qty"] > 0:
                _sm_init(pid, p["entry_price"], p["qty"], atr, mode="TP_ACTIVE")
                tp_mult = 0.5 if current_regime in ("SIDEWAYS", "SIDEWAYS_LOW_VOL") else config.ATR_TP_MULTIPLIER
                oid = await _sm_place_tp(client, pid, p["qty"], p["entry_price"], atr, mult=tp_mult)
                if oid:
                    print(f"  SM INIT: {pid} tp_oid={oid} entry={p['entry_price']:,.0f} atr={atr:.2f}% tp_mult={tp_mult:.1f}", flush=True)
                else:
                    _position_states[pid]["state"] = "PENDING"
                    print(f"  SM PENDING: {pid} — TP fail, retry next cycle", flush=True)

        for pid_sm, sm in list(_position_states.items()):
            if sm["state"] != "TP_ACTIVE":
                continue
            lp_sm = ticker_map.get(pid_sm, {}).get("last") or LIVE_TICKERS.get(pid_sm, {}).get("last", 0)
            if lp_sm <= 0:
                continue
            p_sm = next((x for x in positions if x["pair"] == pid_sm), None)
            if not p_sm:
                continue
            entry_sm = sm["entry_price"]
            atr_sm = sm.get("atr_pct", 1.0)
            if atr_sm > 10 and _latest_ohlcv_map_1h.get(pid_sm):
                recalc = risk.compute_atr(_latest_ohlcv_map_1h[pid_sm])
                if recalc < atr_sm:
                    atr_sm = recalc
            bear_regime = _latest_regime.get("regime", "") in ("BEAR",)
            if bear_regime and sm["state"] == "TP_ACTIVE" and sm.get("tp_order_id"):
                bear_bid = int(ticker_map.get(pid_sm, {}).get("buy", 0) or lp_sm)
                sm_tp = sm.get("tp_price", 0)
                if sm_tp > bear_bid * 1.005:
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
                        print(f"  SM CYCLE → SL: {pid_sm} @ Rp{int(sl_level_sm):,} (price {lp_sm:,.0f})", flush=True)

        sl_hits = []
        for p in list(positions):
            if p["pair"] in _realtime_sold:
                continue
            if p["pair"] in _pending_sells:
                continue
            if _position_states.get(p["pair"]) or p["pair"] in _sm_cooldown:
                continue
            mentry = _momentum_entry_time.get(p["pair"], 0)
            if mentry > 0 and time.time() - mentry < 120:
                continue
            last = ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            atr_val = p.get("atr_pct") or risk.compute_atr(ohlcv_map_1h.get(p["pair"], []))
            result = risk.check_sl_tp(p["entry_price"], last, p["side"], pair=p["pair"], atr_pct=atr_val, entry_mode=p.get("entry_mode", "KONSERVATIF"))
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
                            add_position(positions, p["pair"], p["side"], pyr_spend / pyr_fill if pyr_fill else p["entry_price"],
                                         pyr_fill, pyr_spend, p.get("atr_pct"), time.time(),
                                         p.get("entry_mode", "KONSERVATIF"))
                            actual_idr_balance -= pyr_spend
                            persist.save_positions(positions)
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
                pair_min = _pair_meta.get(p["pair"], {}).get("min_base", config.MIN_ORDER_IDR)
                if dust_value < pair_min:
                    print(f"  {p['pair']}: dust Rp{dust_value:,.0f} < min Rp{pair_min:,} — hapus tracking", flush=True)
                    positions.remove(p)
                    persist.save_positions(positions)
                    continue
                sl_hits.append(f"{p['pair']} {result}: {pnl:+.0f} IDR")
                if result != "INITIAL_SL":
                    _cooldown[p["pair"]] = time.time()
                    print(f"COOLDOWN: {p['pair']} set for 12h", flush=True)
                    await send_message(f"⏳ {p['pair']} cooldown 12 jam")
                if not config.PAPER_TRADING and config.INDODAX_API_KEY:
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
                        _ts_s = int(time.time() * 1000)
                        qty_str = f"{sl_qty:.8f}".rstrip("0").rstrip(".") or "0"
                        sell_bid = int(ticker_map.get(p["pair"], {}).get("buy", last))
                        sell_price = int(sell_bid * (1 + config.MAKER_SLIPPAGE))
                        sp = {"method":"trade","timestamp":_ts_s,"recvWindow":"5000","pair":p["pair"],"type":"sell",
                              coin_name: qty_str, "price": str(sell_price), "order_type":"limit"}
                        sb = urlencode(sp)
                        ss = hmac.new(config.INDODAX_SECRET_KEY.encode(),sb.encode(),hashlib.sha512).hexdigest()
                        sr = await client.post(config.INDODAX_TAPI_URL, headers={
                            "Key":config.INDODAX_API_KEY,"Sign":ss,
                            "Content-Type":"application/x-www-form-urlencoded",
                        }, content=sb)
                        sj = sr.json()
                        if sj.get("success") == 1:
                            remain = float(sj["return"].get(f"remain_{coin_name}", 0))
                            if remain > 0:
                                print(f"  SELL UNFILLED: {p['pair']} — track sampe keisi (max {config.SELL_GRACE_CYCLE} cycle)", flush=True)
                                order_id = sj["return"].get("order_id")
                                if order_id:
                                    _pending_sells[p["pair"]] = {"order_id": order_id, "qty": sl_qty, "amount": sl_qty * last, "price": sell_price, "cycles": 0, "pair": p["pair"]}
                                continue
                            print(f"  SOLD {p['pair']} via maker", flush=True)
                            positions.remove(p)
                            persist.save_positions(positions)
                            sell_value = last * p["qty"]
                            log_trade("sell", last, p["qty"], sell_value,
                                      status="closed", pnl=pnl, reason=f"{result} {p['pair']}")
                            if config.AUTO_COMPOUND:
                                _realized_pnl_idr += pnl
                        else:
                            err_msg = sj.get('error', 'unknown')
                            print(f"  Sell {p['pair']} failed: {err_msg} — CIO will decide", flush=True)
                    except Exception as e:
                        print(f"  Auto-sell failed {p['pair']}: {e}", flush=True)
                if p["pair"] in _tp_limit_orders:
                    try:
                        oid = _tp_limit_orders.pop(p["pair"])
                        cancel_params = {
                            "method": "cancelOrder", "timestamp": int(time.time() * 1000),
                            "recvWindow": "5000", "pair": p["pair"],
                            "order_id": str(oid), "type": "sell",
                        }
                        cancel_body = urlencode(cancel_params)
                        cancel_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body.encode(), hashlib.sha512).hexdigest()
                        await client.post(config.INDODAX_TAPI_URL, headers={
                            "Key": config.INDODAX_API_KEY, "Sign": cancel_sig,
                            "Content-Type": "application/x-www-form-urlencoded",
                        }, content=cancel_body)
                        print(f"  TP ORDER CANCELLED for {p['pair']} (order_id={oid})", flush=True)
                    except Exception as e:
                        print(f"  Cancel TP failed {p['pair']}: {e}", flush=True)

        for sl_hit in sl_hits:
            if "SL_HIT" in sl_hit or "TRAILING_SL" in sl_hit or "INITIAL_SL" in sl_hit or "ATR_SL" in sl_hit:
                pair = sl_hit.split(" ")[0].replace(":", "")
                if _rothschild_active:
                    _cooldown[pair] = time.time() + 3600
                    print(f"COOLDOWN: {pair} 60 menit (Rothschild mode)", flush=True)
                else:
                    _coin_blacklist.add(pair)
                    print(f"BLACKLIST: {pair} added (hit stop loss)", flush=True)
                    await send_message(f"⛔ {pair} blacklist (kena SL)")
        if len(_coin_blacklist) > 20:
            oldest = next(iter(_coin_blacklist))
            _coin_blacklist.discard(oldest)
        persist.save_blacklist(_coin_blacklist)
        persist.save_cooldown(_cooldown)
        persist.save_sm_cooldown(_sm_cooldown)
        now_rs = time.time()
        _realtime_sold -= {p for p in _realtime_sold if now_rs - _realtime_sold_time.get(p, 0) > 300}

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
                if positions and config.INDODAX_API_KEY:
                    pair_str = ",".join(p["pair"] for p in positions[:5])
                    await refresh_deadman(client, pair_str)
                return

        all_held = {p["pair"] for p in positions}
        bot_pair_set = {p["pair"] for p in positions}
        trades = [t for t in trades if t.get("action") != "SELL" or t["pair"] in all_held]
        for t in list(trades):
            if t.get("action") == "SELL":
                sell_pair = t["pair"]
                match = next((p for p in positions if p["pair"] == sell_pair), None)
                if not match:
                    continue
                price_now = LIVE_TICKERS.get(sell_pair, {}).get("last") or ticker_map.get(sell_pair, {}).get("last", 0)
                entry = match.get("entry_price", 0)
                pnl = (price_now - entry) / entry * 100 if entry else 0
        if cycle_counter <= 1:
            if any(t.get("action") == "SELL" for t in decision.get("trades", [])):
                print("STARTUP GUARD: blocked CIO sells (positions restored from balance)", flush=True)
            trades = [t for t in trades if t.get("action") != "SELL"]
        trades = [t for t in trades if t.get("action") != "BUY" or (t["pair"] not in _coin_blacklist and (t["pair"] not in _cooldown or time.time() >= _cooldown.get(t["pair"], 0)) and (t["pair"] not in _sm_cooldown or time.time() >= _sm_cooldown.get(t["pair"], 0)))]
        if config.SKIP_COINS:
            trades = [t for t in trades if t.get("action") != "BUY" or t["pair"] not in config.SKIP_COINS]
        if _coin_blacklist:
            blocked = [t for t in decision.get("trades", []) if t.get("action") == "BUY" and t["pair"] in _coin_blacklist]
            if blocked:
                print(f"BLACKLIST: Skipped BUY for {', '.join(t['pair'] for t in blocked)}", flush=True)
        for p in list(positions):
            last_p = await _coin_price(p["pair"]) or LIVE_TICKERS.get(p["pair"], {}).get("last") or 0
            if last_p == 0:
                try:
                    t = await fetch_ticker(client, pair=p["pair"])
                    if t: last_p = t.get("last", 0)
                except Exception:
                    pass
            if last_p <= 0 and p["pair"] not in _realtime_sold and p["pair"] not in _position_states and p["pair"] not in _sm_cooldown:
                print(f"  FORCE SELL {p['pair']}: harga 0 — force close (tidak SM managed)", flush=True)
                trades.append({"pair": p["pair"], "action": "SELL", "allocation_pct": 100, "reason": "Force close no price"})

        selling_pairs = {t["pair"] for t in trades if t.get("action") == "SELL"}
        extra_buys = [t for t in trades if t.get("action") == "BUY" and t["pair"] in all_held]
        new_buys = [t for t in trades if t.get("action") == "BUY" and t["pair"] not in all_held]
        slots_left = max(0, max_positions - len(all_held - selling_pairs))
        if len(new_buys) > slots_left:
            trades = [t for t in trades if t.get("action") == "SELL"] + extra_buys + new_buys[:slots_left]
            print(f"Limited new buys to {slots_left} (max {max_positions} unique, equity Rp{total_equity:,.0f})", flush=True)

        if not trades:
            print(f"Cycle done in {int(time.time() - _t0)}s. Sleeping.", flush=True)
            persist.save_initial_equity(total_equity)
            if positions and config.INDODAX_API_KEY:
                pair_str = ",".join(p["pair"] for p in positions[:5])
                await refresh_deadman(client, pair_str)
            return

        valid_trades = portfolio_risk.validate_allocation(trades, current_positions_info, balance_idr)
        if not valid_trades:
            print("No valid trades after risk checks.", flush=True)
            print(f"Cycle done in {int(time.time() - _t0)}s. Sleeping.", flush=True)
            return

        correlated = correlation_risk(positions + [{"pair": t["pair"], "entry_price": 0} for t in valid_trades if t.get("action") == "BUY"], ticker_map)
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

            match = next((p for p in positions if p["pair"] == pid), None)
            if action == "SELL" and match:
                sell_qty_raw = match["qty"]
                if not config.PAPER_TRADING and config.INDODAX_API_KEY:
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
            atr_pct = risk.compute_atr(ohlcv) if ohlcv else None
            if action == "BUY" and atr_pct:
                raw_atr = risk.compute_atr(ohlcv, clamped=False)
                vol_idr = float(ticker.get("vol_idr", 0))
                if raw_atr < 1.5:
                    print(f"  {pid}: ATR {raw_atr:.1f}% < 1.5 — skip (terlalu stabil)", flush=True)
                    continue
                if raw_atr > 25.0 or vol_idr < 500_000_000:
                    print(f"  {pid}: ATR {raw_atr:.1f}% vol Rp{vol_idr:,.0f} — skip (terlalu berisiko)", flush=True)
                    continue
            if not risk.is_profit_viable(price, qty, action, atr_pct=atr_pct):
                print(f"  {pid}: skipped - fees eat profit", flush=True)
                continue

            print(f"  {action} {pid} @ {price} | Rp{amount:,.0f} ({qty:.6f}) | alloc: {alloc}%", flush=True)

            tp_limit_price = 0
            if atr_pct and action == "BUY":
                sl, tp = risk.get_sl_tp(price, action, atr_pct)
                tp_limit_price = int(tp)
                print(f"  ATR: {atr_pct}% | SL: {sl} | TP: {tp}", flush=True)

            if action == "SELL" and _position_states.get(pid):
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
                last_err = _order_error_cooldown.get(pid, 0)
                if now_t - last_err > 1800:
                    _order_error_cooldown[pid] = now_t
                    await send_message(f"Order failed {pid}: {err_str}")
                if action == "SELL" and "insufficient" in err_str.lower():
                    positions = [p for p in positions if p["pair"] != pid]
                    persist.save_positions(positions)
                    _tp_limit_orders.pop(pid, None)
                    print(f"  REMOVED {pid} from tracking (sell failed)", flush=True)
                continue

            is_pending_maker = action == "BUY" and ot == "maker_first" and order.get("order_id") and float(order.get("remain_rp", order.get("remain_idr", 0)) or 0) > 0
            if is_pending_maker:
                _pending_orders[pid] = {
                    "order_id": order["order_id"], "is_maker": True,
                    "side": "BUY", "price": price, "qty": qty,
                    "amount_idr": amount, "atr_pct": atr_pct if ohlcv else None,
                    "cycles": 0,
                }
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
                _latest_balance = actual_idr_balance
                add_position(positions, pid, action, actual_price, actual_qty, actual_spend,
                             atr_pct if ohlcv else None, time.time(),
                             "ROTHSCHILD" if _rothschild_active else "KONSERVATIF")
                persist.save_positions(positions)
                _ext_entry_prices[pid] = actual_price
                persist.save_entry_prices(_ext_entry_prices)
            elif action == "SELL":
                actual_received = float(order.get("receive_rp", 0)) or amount
                actual_qty = float(order.get(f"spend_{coin_name}", 0)) or qty
                actual_sell_price = actual_received / actual_qty if actual_qty else price
                t["exec_price"] = actual_sell_price
                actual_idr_balance += actual_received
                _latest_balance = actual_idr_balance
                positions = [p for p in positions if p["pair"] != pid]
                persist.save_positions(positions)
                sell_entry = t.get("entry_price", 0) or 0
                sell_cost = sell_entry * actual_qty
                sell_pnl_idr = actual_received - sell_cost
                if sell_pnl_idr > 0:
                    _cio_stats["wins"] += 1
                else:
                    _cio_stats["losses"] += 1
                if config.AUTO_COMPOUND:
                    _realized_pnl_idr += sell_pnl_idr
            pnl_trade = (t.get("exec_price", 0) - t.get("entry_price", 0)) / t.get("entry_price", 1) * 100 if t.get("entry_price") else 0
            _recent_actions.append({
                "time": time.time(), "action": action, "pair": pid,
                "price": t.get("exec_price", price), "pnl": round(pnl_trade, 2),
                "qty": actual_qty if action == "SELL" else (actual_qty if action == "BUY" else qty),
            })
            if len(_recent_actions) > 50:
                _recent_actions.pop(0)
            executed_trades.append(t)

            if action == "SELL":
                _pending_orders.pop(pid, None)
                _tp_limit_orders.pop(pid, None)
                if pid in _tp_limit_orders and not config.PAPER_TRADING and config.INDODAX_API_KEY:
                    try:
                        oid = _tp_limit_orders.pop(pid)
                        cancel_params = {
                            "method": "cancelOrder", "timestamp": int(time.time() * 1000),
                            "recvWindow": "5000", "pair": pid,
                            "order_id": str(oid), "type": "sell",
                        }
                        cancel_body = urlencode(cancel_params)
                        cancel_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body.encode(), hashlib.sha512).hexdigest()
                        await client.post(config.INDODAX_TAPI_URL, headers={
                            "Key": config.INDODAX_API_KEY, "Sign": cancel_sig,
                            "Content-Type": "application/x-www-form-urlencoded",
                        }, content=cancel_body)
                        print(f"  TP LIMIT ORDER cancelled for {pid} (order_id={oid})", flush=True)
                    except Exception as e:
                        print(f"  Cancel TP limit failed {pid}: {e}", flush=True)

        if executed_trades:
            _cio_stats["total_decisions"] += 1
            for t in executed_trades:
                _cio_stats["buys" if t.get("action") == "BUY" else "sells"] += 1

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
            msg_lines.append(f"Total posisi: {len(positions)} | Cash: Rp{actual_idr_balance:,.0f} (budget: Rp{balance_idr:,})")
            await send_message("\n".join(msg_lines))
            print(f"Portfolio: {len(positions)} total positions | Cash: Rp{actual_idr_balance:,.0f} (budget: Rp{balance_idr:,})", flush=True)

        seen = set()
        deduped = []
        for p in positions:
            if p["pair"] not in seen:
                seen.add(p["pair"])
                deduped.append(p)
        if len(deduped) != len(positions):
            positions[:] = deduped
            persist.save_positions(positions)
            print(f"DEDUP: removed {len(positions) - len(deduped)} duplicate positions", flush=True)

        if positions and config.INDODAX_API_KEY:
            pair_str = ",".join(p["pair"] for p in positions[:5])
            await refresh_deadman(client, pair_str)

        if cycle_counter % 10 == 0 and config.INDODAX_API_KEY:
            try:
                clean_ts = int(time.time() * 1000)
                clean_params = {"method": "openOrders", "timestamp": str(clean_ts), "recvWindow": "5000"}
                clean_body = urlencode(clean_params)
                clean_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), clean_body.encode(), hashlib.sha512).hexdigest()
                clean_r = await client.post(config.INDODAX_TAPI_URL, headers={
                    "Key": config.INDODAX_API_KEY, "Sign": clean_sig,
                    "Content-Type": "application/x-www-form-urlencoded",
                }, content=clean_body)
                clean_data = clean_r.json()
                if clean_data.get("success") == 1:
                    tracked = {p["pair"] for p in positions} | set(_pending_orders.keys())
                    orders_map = clean_data["return"].get("orders", {})
                    if isinstance(orders_map, dict):
                        for opair, olist in orders_map.items():
                            if isinstance(olist, list):
                                for o in olist:
                                    if opair not in tracked:
                                        oid = o.get("order_id")
                                        if oid:
                                            cancel_cbody = urlencode({
                                                "method": "cancelOrder", "timestamp": int(time.time() * 1000),
                                                "recvWindow": "5000", "pair": opair,
                                                "order_id": str(oid), "type": o.get("type", "buy"),
                                            })
                                            cancel_csig = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_cbody.encode(), hashlib.sha512).hexdigest()
                                            await client.post(config.INDODAX_TAPI_URL, headers={
                                                "Key": config.INDODAX_API_KEY, "Sign": cancel_csig,
                                                "Content-Type": "application/x-www-form-urlencoded",
                                            }, content=cancel_cbody)
                                            print(f"CLEANUP: cancelled orphan {opair} {o.get('type')} (order_id={oid})", flush=True)
            except Exception as e:
                print(f"Orphan cleanup error: {e}", flush=True)

        _latest_regime = regime_info
        _latest_ticker_map = ticker_map
        _latest_all_signals = all_signals
        _latest_ohlcv_map_1h = ohlcv_map_1h
        _latest_balance = actual_idr_balance

        if config.AUTO_COMPOUND and _realized_pnl_idr != 0:
            old_cap = config.PLAY_CAPITAL_IDR
            config.PLAY_CAPITAL_IDR = max(
                config.PLAY_CAPITAL_IDR + _realized_pnl_idr,
                config.MIN_ORDER_IDR
            )
            if config.COMPOUND_CAP_IDR > 0:
                config.PLAY_CAPITAL_IDR = min(config.PLAY_CAPITAL_IDR, config.COMPOUND_CAP_IDR)
            sign = "+" if _realized_pnl_idr >= 0 else ""
            print(f"COMPOUND: {sign}Rp{_realized_pnl_idr:,.0f} (Rp{old_cap:,.0f} → Rp{config.PLAY_CAPITAL_IDR:,.0f})", flush=True)
            _realized_pnl_idr = 0.0

    except Exception as e:
        print(f"Portfolio cycle error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            now_e = time.time()
            if now_e - _order_error_cooldown.get("cycle_error", 0) > 1800:
                _order_error_cooldown["cycle_error"] = now_e
                await send_message(f"Portfolio cycle error: {e}")
        except Exception:
            pass
    finally:
        global _cycle_last_end, _cycle_last_info
        _cycle_last_end = time.time()
        _cycle_last_info = {
            "cycle": cycle_counter,
            "regime": _latest_regime.get("regime", "?"),
            "cash": actual_idr_balance,
            "positions": len(positions),
            "duration": int(time.time() - _t0),
        }
        print(f"⏱ Cycle #{cycle_counter} finished in {int(time.time() - _t0)}s", flush=True)

_latest_balance: float = 0

async def _optimizer_loop():
    _warned_no_key = False
    while not shutdown_flag:
        await asyncio.sleep(15)
        if not config.DEEPSEEK_API_KEY:
            if not _warned_no_key and cycle_counter > 0:
                _warned_no_key = True
                await send_message("⚠️ AI OPTIMIZER: DeepSeek API key tidak terisi/balance habis — optimasi nonaktif. Bot tetap jalan dengan parameter terakhir.")
            continue
        if cycle_counter == 0:
            continue
        opt_state = persist.load_optimizer_state()
        last_id = opt_state.get("last_trade_id", 0)
        last_run = opt_state.get("last_run_time", 0)
        new_count = count_new_completed_sells(last_id)
        seven_days = 7 * 86400
        if new_count < 50 and (time.time() - last_run) < seven_days:
            continue
        try:
            recent_sells = get_recent_completed_sells(100)
            eq_curve = persist.load_equity_curve()
            print(f"  AI Optimizer: {new_count} new sells since last run — running analysis ({len(recent_sells)} total)", flush=True)
            msg = await optimizer.run(recent_sells, eq_curve, regime_history)
            if msg:
                await send_message(msg)
            latest_id = get_max_trade_id()
            persist.save_optimizer_state({"last_trade_id": latest_id, "last_run_time": time.time()})
        except Exception as opt_e:
            print(f"  AI Optimizer error: {opt_e}", flush=True)

async def _balance_poller(client: httpx.AsyncClient):
    global _latest_balance
    while not shutdown_flag:
        try:
            info = await get_balance(client)
            _latest_balance = float(info.get("balance", {}).get("idr", 0))
        except Exception as e:
            print(f"Balance poller error: {e}", flush=True)
        await asyncio.sleep(30)

async def _momentum_scanner():
    _last_scan_pairs: dict[str, float] = {}
    while not shutdown_flag:
        try:
            await asyncio.sleep(config.MOMENTUM_SCAN_INTERVAL)
            if not _latest_ohlcv_map_1h:
                continue
            if _daily_loss_hit_today:
                print(f"  Momentum scanner: daily loss hit — skip entry", flush=True)
                continue
            regime_now = _latest_regime.get("regime", "")
            if regime_now in ("BEAR", "HIGH_VOL"):
                print(f"  Momentum scanner: {regime_now} — skip entry", flush=True)
                continue
            from db import get_trade_count_today
            if get_trade_count_today() >= config.MAX_DAILY_TRADES:
                print(f"  Momentum scanner: max trades/day ({config.MAX_DAILY_TRADES}) — skip", flush=True)
                continue
            cash_avail = _latest_balance
            if cash_avail < config.MIN_ORDER_IDR:
                continue
            max_pos = config.MAX_OPEN_POSITIONS if not _rothschild_active else config.ROTHSCHILD_OPEN_POSITIONS
            if len(positions) >= max_pos:
                continue
            for pid, ohlcv in list(_latest_ohlcv_map_1h.items()):
                if len(positions) >= max_pos:
                    break
                if any(p["pair"] == pid for p in positions):
                    continue
                if pid in config.STABLECOINS or pid in config.SKIP_COINS:
                    continue
                if pid in _coin_blacklist or pid in _realtime_sold:
                    continue
                if pid in _cooldown and time.time() < _cooldown[pid]:
                    continue
                if time.time() - _last_scan_pairs.get(pid, 0) < 30:
                    continue
                price = LIVE_TICKERS.get(pid, {}).get("last") or _latest_ticker_map.get(pid, {}).get("last", 0)
                if not price or price < 20:
                    continue
                signal = momentum_engine.evaluate(pid, ohlcv, price)
                if not signal:
                    continue
                _last_scan_pairs[pid] = time.time()
                print(f"  MOMENTUM: {pid} {signal}", flush=True)
                atr_chk = risk.compute_atr(ohlcv, clamped=False)
                if atr_chk < 1.5:
                    print(f"    ATR {atr_chk:.1f}% < 1.5 — skip (terlalu stabil)", flush=True)
                    continue
                if atr_chk > 25.0:
                    print(f"    ATR {atr_chk:.1f}% > 25 — skip", flush=True)
                    continue
                vol_idr = float(_latest_ticker_map.get(pid, {}).get("vol_idr", 0) or 0)
                if vol_idr < 500_000_000:
                    print(f"    Vol Rp{vol_idr:,.0f} < 500M — skip", flush=True)
                    continue
                if len(ohlcv) >= 15:
                    pcloses = [float(c["close"]) for c in ohlcv[-30:]]
                    pat = patterns.detect_paradiddle(pcloses)
                    if pat in ("FAKE_BREAKOUT_SELL", "EXHAUSTION_SELL"):
                        print(f"    Pattern {pat} — skip", flush=True)
                        continue
                if pid in _pair_meta:
                    try:
                        async with httpx.AsyncClient() as _obc:
                            _ob = await fetch_orderbook(_obc, pair=pid, depth=3)
                        if _ob and _ob.get("imbalance_pct", 0) < -10:
                            print(f"    Book sell pressure {_ob['imbalance_pct']:.0f}% — skip", flush=True)
                            continue
                    except Exception:
                        pass
                if pid in _sm_cooldown and _sm_cooldown[pid] > time.time():
                    print(f"    Cooldown: {pid} — skip", flush=True)
                    continue
                if len(ohlcv) >= 5:
                    hs_m = [float(x["high"]) for x in ohlcv[-14:]]
                    ls_m = [float(x["low"]) for x in ohlcv[-14:]]
                    r_m = max(hs_m) - min(ls_m)
                    if r_m > 0:
                        pp_m = (price - min(ls_m)) / r_m * 100
                        if pp_m > 75 or pp_m < 25:
                            print(f"    Range filter: {pid} pp={pp_m:.0f}% {'>75 (puncak)' if pp_m > 75 else '<25 (jatuh)'} — skip", flush=True)
                            continue
                        print(f"    Range filter: {pid} pp={pp_m:.0f}% (25-75) — OK", flush=True)
                alloc = 0.4
                amount = int(cash_avail * alloc)
                if amount < 25000:
                    continue
                qty = amount / price
                if not risk.is_profit_viable(price, qty, "BUY", atr_pct=atr_chk):
                    print(f"    Fee makan profit — skip", flush=True)
                    continue
                try:
                    async with httpx.AsyncClient() as _mc:
                        order = await place_order(_mc, "buy", price, amount, pair=pid, order_type="market")
                    if order.get("order_id") or order.get("receive_rp"):
                        coin_name = pid.split("_")[0]
                        actual_qty = float(order.get(f"receive_{coin_name}", 0)) or qty
                        actual_spend = float(order.get("spend_rp", 0)) or amount
                        add_position(positions, pid, "BUY", actual_spend / actual_qty if actual_qty else price,
                                     actual_qty, actual_spend, None, time.time(),
                                     "ROTHSCHILD" if _rothschild_active else "KONSERVATIF")
                        persist.save_positions(positions)
                        _ext_entry_prices[pid] = actual_spend / actual_qty if actual_qty else price
                        persist.save_entry_prices(_ext_entry_prices)
                        cash_avail -= actual_spend
                        await send_message(f"⚡ MOMENTUM {signal}: BUY {pid}\nRp{actual_spend:,.0f} @ {price:,.0f}")
                        _momentum_entry_time[pid] = time.time()
                        print(f"    EXECUTED: BUY {pid}", flush=True)
                except Exception as ex:
                    print(f"    Order failed: {ex}", flush=True)
        except Exception as e:
            print(f"Momentum scanner error: {e}", flush=True)

async def _realtime_sltp_check(pair: str, price: float):
    global _realized_pnl_idr, _latest_balance, _pyramid_cooldown
    now_t = time.time()
    if now_t - _realtime_sltp_last.get(pair, 0) < 10:
        return
    _realtime_sltp_last[pair] = now_t
    if shutdown_flag:
        return
    sm = _position_states.get(pair)
    if not sm:
        return
    pos_check = next((x for x in positions if x["pair"] == pair), None)
    if not pos_check:
        _sm_cleanup(pair)
        return
    entry = sm["entry_price"]
    atr = sm.get("atr_pct", 1.0)
    if atr > 10 and _latest_ohlcv_map_1h.get(pair):
        recalc = risk.compute_atr(_latest_ohlcv_map_1h[pair])
        if recalc < atr:
            atr = recalc
    sl_level = entry * (1 - max(atr, 0.5) * config.ATR_SL_MULTIPLIER / 100)
    recovery_level = entry * 1.005

    if sm["state"] == "TP_ACTIVE":
        if price >= entry * 1.025 and _latest_regime.get("regime") == "BULL" and (sm.get("tp_price", 0) == 0 or price < sm["tp_price"] * 0.995):
            async with httpx.AsyncClient() as c:
                if sm.get("tp_order_id"):
                    await _sm_cancel(c, sm["tp_order_id"], pair)
                p = next((x for x in positions if x["pair"] == pair), None)
                if p:
                    oid = await _sm_place_sl(c, pair, p["qty"], price, atr, mult=config.ROTHSCHILD_TRAILING_SL_ATR)
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
                p = next((x for x in positions if x["pair"] == pair), None)
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
                p = next((x for x in positions if x["pair"] == pair), None)
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
            if sm.get("sl_order_id") and trail_price > sm.get("sl_price", 0):
                async with httpx.AsyncClient() as c:
                    await _sm_cancel(c, sm["sl_order_id"], pair)
                    p = next((x for x in positions if x["pair"] == pair), None)
                    if p:
                        oid = await _sm_place_sell(c, pair, p["qty"], trail_price)
                        if oid:
                            sm["sl_order_id"] = oid
                            sm["sl_price"] = trail_price
                            print(f"  SM TRAIL: {pair} trailing_high={price:,.0f} sl={trail_price:,} (oid={oid})", flush=True)
        if pnl_pct >= max(atr, 0.5) * config.ROTHSCHILD_PYRAMID_TRIGGER / 100:
            now_pyr = time.time()
            if now_pyr - _pyramid_cooldown.get(pair, 0) < 300:
                print(f"  SM PYRAMID SKIP: {pair} cooldown ({int(now_pyr - _pyramid_cooldown.get(pair, 0))}s)", flush=True)
                return
            pyr_pos = next((x for x in positions if x["pair"] == pair), None)
            if not pyr_pos:
                print(f"  SM PYRAMID SKIP: {pair} posisi tidak ditemukan (mungkin sudah kejual)", flush=True)
                _sm_cleanup(pair)
                return
            if not _daily_loss_hit_today or _greed_used_today:
                pyr_amt = int(max(config.MIN_ORDER_IDR, _latest_balance * config.ROTHSCHILD_PYRAMID_MULT))
                if pyr_amt >= config.MIN_ORDER_IDR and pyr_amt <= _latest_balance:
                    async with httpx.AsyncClient() as _pc:
                        try:
                            pyr_order = await place_order(_pc, "buy", price, pyr_amt, pair=pair, order_type="market")
                            if pyr_order.get("order_id") or pyr_order.get("receive_rp"):
                                coin_n = pair.split("_")[0]
                                pyr_f = float(pyr_order.get(f"receive_{coin_n}", 0)) or (pyr_amt / price)
                                pyr_spend = float(pyr_order.get("spend_rp", 0)) or pyr_amt
                                _latest_balance -= pyr_spend
                                old_qty = sm["qty"]
                                old_entry = sm["entry_price"]
                                total_qty = old_qty + pyr_f
                                new_entry = (old_entry * old_qty + price * pyr_f) / total_qty if total_qty > 0 else price
                                sm["entry_price"] = new_entry
                                sm["qty"] = total_qty
                                add_position(positions, pair, "BUY", price, pyr_f, pyr_spend, atr, time.time(), "TRAILING")
                                _pyramid_cooldown[pair] = now_pyr
                                if sm.get("sl_order_id"):
                                    oid = sm["sl_order_id"]
                                    cb_p = urlencode({"method":"cancelOrder","timestamp":int(time.time()*1000),"recvWindow":"5000","pair":pair,"order_id":str(oid),"type":"sell"})
                                    cs_p = hmac.new(config.INDODAX_SECRET_KEY.encode(), cb_p.encode(), hashlib.sha512).hexdigest()
                                    await _pc.post(config.INDODAX_TAPI_URL, headers={"Key":config.INDODAX_API_KEY,"Sign":cs_p,"Content-Type":"application/x-www-form-urlencoded"}, content=cb_p)
                                    sm["sl_order_id"] = None
                                p_sm = next((x for x in positions if x["pair"] == pair), None)
                                if p_sm:
                                    new_oid = await _sm_place_sl(_pc, pair, p_sm["qty"], new_entry, atr, mult=config.ROTHSCHILD_TRAILING_SL_ATR)
                                    if new_oid:
                                        sm["sl_order_id"] = new_oid
                                print(f"  SM PYRAMID: {pair} +{pyr_f:.6f} @ {price:,.0f} avg_entry={new_entry:,.0f}", flush=True)
                                await send_message(f"🔺 SM PYRAMID: {pair} +{pyr_f:.6f} @ Rp{price:,.0f}")
                        except Exception as e:
                            print(f"  SM pyramid fail {pair}: {e}", flush=True)

async def main():
    global _latest_balance, shutdown_flag

    if config.INDODAX_API_KEY:
        try:
            async with httpx.AsyncClient() as _bc:
                info = await get_balance(_bc)
                _latest_balance = float(info.get("balance", {}).get("idr", 0))
        except Exception:
            pass

    print("=" * 50, flush=True)
    print("  FMA ALPHA QUANT LABS — INDODAX", flush=True)
    print(f"  Target: Rp237k → Rp1.000.000 🚀", flush=True)
    print(f"  Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}", flush=True)
    print(f"  Rules Engine manages play capital dynamically", flush=True)
    print(f"  Model: {config.DEEPSEEK_MODEL}", flush=True)
    print(f"  Max positions: {config.MAX_OPEN_POSITIONS} (Rothschild: {config.ROTHSCHILD_OPEN_POSITIONS})", flush=True)
    print(f"  Rules Engine scans top {config.MAX_SCAN_PAIRS} by volume", flush=True)
    mode_label = "ALPHA" if config.ALPHA_MODE else ("INSANE" if config.INSANE_MODE else "STANDARD")
    print(f"  Mode: {'🔴' if config.ALPHA_MODE or config.INSANE_MODE else ''} {mode_label} | SL ATR×{config.ATR_SL_MULTIPLIER:.0f} | TP ATR×{config.ATR_TP_MULTIPLIER:.0f}", flush=True)
    print("=" * 50, flush=True)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        init_db()
        init_chat_db()
        saved = persist.load_positions()
        if saved:
            positions.extend(saved)
        saved_tp = persist.load_today_peak()
        if saved_tp > risk.today_peak and 1_000 < saved_tp < 100_000_000_000:
            risk.today_peak = saved_tp
            print(f"  Restored today_peak from persist: Rp{risk.today_peak:,.0f}", flush=True)
        print("DB init OK", flush=True)
        if os.getenv("CLEAR_LOSS_HOLD"):
            persist.save_daily_loss_hit(False)
            persist.save_loss_hit_date("")
            _daily_loss_hit_today = False
            print(f"  CLEAR_LOSS_HOLD: daily loss flag reset by env", flush=True)
        _daily_loss_hit_today = persist.load_daily_loss_hit()
        if _daily_loss_hit_today:
            print(f"  Previous session hit daily loss — TP allow, SL hold", flush=True)
        _ext_entry_prices.update(persist.load_entry_prices())
        print(f"Loaded {len(_ext_entry_prices)} entry prices from DB", flush=True)
        recent = get_recent_trades(limit=100)
        portfolio_risk.set_trade_history(recent)
        print(f"Kelly: {len(recent)} trades loaded, optimal f={portfolio_risk.kelly.optimal_fraction():.2f}", flush=True)
        saved_blacklist = persist.load_blacklist()
        if saved_blacklist:
            _coin_blacklist.update(saved_blacklist)
            print(f"Restored {len(_coin_blacklist)} blacklisted pairs", flush=True)
        saved_cooldown = persist.load_cooldown()
        if saved_cooldown:
            now = time.time()
            _cooldown.update({k: v for k, v in saved_cooldown.items() if v > now})
            print(f"Restored {len(_cooldown)} active cooldowns", flush=True)
        saved_sm_cd = persist.load_sm_cooldown()
        if saved_sm_cd:
            now = time.time()
            _sm_cooldown.update({k: v for k, v in saved_sm_cd.items() if v > now})
            print(f"Restored {len(_sm_cooldown)} active SM cooldowns", flush=True)
        cb = persist.load_circuit_breaker()
        _cb_consecutive_loss_days = cb.get("consecutive_loss_days", 0)
        _cb_last_loss_date = cb.get("last_loss_date", "")
        _cb_active_until = cb.get("active_until", 0)
        if _cb_active_until > time.time():
            remaining_h = int((_cb_active_until - time.time()) / 3600)
            print(f"🛑 CIRCUIT BREAKER aktif — {_cb_consecutive_loss_days} hari loss berturut, sisa {remaining_h} jam cooldown", flush=True)
        elif _cb_active_until > 0:
            print(f"✅ CIRCUIT BREAKER selesai — session sebelumnya udah cooldown", flush=True)
            _cb_consecutive_loss_days = 0
            _cb_active_until = 0
            persist.save_circuit_breaker({"consecutive_loss_days": 0, "last_loss_date": "", "triggered_at": 0, "active_until": 0})
        else:
            print(f"Circuit breaker: {_cb_consecutive_loss_days} consecutive loss days on record", flush=True)
    except Exception as e:
        print(f"DB init failed: {e}", flush=True)

    if config.INDODAX_API_KEY and not config.PAPER_TRADING:
        try:
            async with httpx.AsyncClient(timeout=10) as _cc:
                ts_now = int(time.time() * 1000)
                oo_params = {"method": "openOrders", "timestamp": str(ts_now), "recvWindow": "5000"}
                oo_body = urlencode(oo_params)
                oo_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), oo_body.encode(), hashlib.sha512).hexdigest()
                oo_r = await _cc.post(config.INDODAX_TAPI_URL, headers={
                    "Key": config.INDODAX_API_KEY, "Sign": oo_sig,
                    "Content-Type": "application/x-www-form-urlencoded",
                }, content=oo_body)
                oo_data = oo_r.json()
                if oo_data.get("success") == 1:
                    tracked_pairs = {p["pair"] for p in positions}
                    orders_data = oo_data["return"].get("orders", {})
                    orders_by_pair = orders_data if isinstance(orders_data, dict) else {}
                    for opair, olist in orders_by_pair.items():
                        if isinstance(olist, list):
                            for o in olist:
                                if o.get("type") == "sell" and opair not in tracked_pairs:
                                    oid = o.get("order_id")
                                    cancel_params = {
                                        "method": "cancelOrder", "timestamp": str(int(time.time() * 1000)),
                                        "recvWindow": "5000", "pair": opair,
                                        "order_id": str(oid), "type": "sell",
                                    }
                                    cancel_body = urlencode(cancel_params)
                                    cancel_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body.encode(), hashlib.sha512).hexdigest()
                                    await _cc.post(config.INDODAX_TAPI_URL, headers={
                                        "Key": config.INDODAX_API_KEY, "Sign": cancel_sig,
                                        "Content-Type": "application/x-www-form-urlencoded",
                                    }, content=cancel_body)
                                    print(f"CLEANUP: cancelled orphan sell {opair} (order_id={oid})", flush=True)
        except Exception as e:
            print(f"Order cleanup: {e}", flush=True)

    mode_n = "INSANE 🚀" if config.INSANE_MODE else ("ALPHA 🔴" if config.ALPHA_MODE else "STANDARD")
    ok = await send_message(
        f"🤖 FMA ALPHA QUANT LABS started (Engineering Mode)\n"
        f"Target: Rp410k → Rp1jt 🚀\n"
        f"Rules Engine — top {config.MAX_SCAN_PAIRS} pairs by volume\n"
        f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'} | {mode_n}\n"
        f"SL ATR×{config.ATR_SL_MULTIPLIER:.0f} | TP ATR×{config.ATR_TP_MULTIPLIER:.0f}\n"
        f"Notifikasi hanya event-based (no spam tiap 5 menit)"
    )
    print(f"Telegram: {'OK' if ok else 'FAILED'}", flush=True)

    async def _build_coin_detail(pair: str) -> str:
        pid = (pair if pair.endswith("_idr") else f"{pair}_idr").lower()
        sig = _latest_all_signals.get(pid, {})
        t = _latest_ticker_map.get(pid, {})
        lt = LIVE_TICKERS.get(pid, {})
        p = next((x for x in positions if x["pair"] == pid), None)
        ohlcv = _latest_ohlcv_map_1h.get(pid, [])
        atr_val = risk.compute_atr(ohlcv) if ohlcv else None
        lines = [f"-- {pid} Detail --"]
        if lt.get("last"):
            lines.append(f"Harga: Rp{lt['last']:,.0f} | 24h: {lt.get('change_24h', 0):+.2f}%")
        elif t.get("last"):
            lines.append(f"Harga: Rp{t['last']:,.0f}")
        if atr_val:
            lines.append(f"ATR: {atr_val:.2f}% | SL: {atr_val*2:.1f}% | TP: {atr_val*3:.1f}%")
        if sig.get("raw_signal"):
            lines.append(f"Signal: {sig['raw_signal']}({sig.get('score',0)}) | 4h:{sig.get('4h_signal','?')} | TF align:{'Y' if sig.get('timeframe_aligned') else 'N'}")
        if sig.get("volume_ratio", 0) > 1:
            lines.append(f"Volume: x{sig['volume_ratio']:.1f} avg")
        if p:
            lp = lt.get("last") or t.get("last") or p.get("entry_price", 0)
            pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
            lines.append(f"Posisi: {p['qty']:.4f} @ {p.get('entry_price',0):,.0f} ({pnl:+.2f}%)")
        return "\n".join(lines) or f"{pid}: tidak ditemukan"

    async def _reply(cid: int, text: str):
        try:
            async with httpx.AsyncClient() as cc:
                r = await cc.post(
                    f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": text},
                )
                if r.status_code != 200:
                    print(f"Telegram reply error: {r.status_code} {r.text[:100]}", flush=True)
        except Exception as e:
            print(f"Telegram reply exception: {e}", flush=True)

    async def telegram_poller():
        global _latest_regime
        last_id = 0
        while not shutdown_flag:
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates",
                        json={"offset": last_id + 1, "timeout": 30},
                    )
                    if r.status_code == 200:
                        for upd in r.json().get("result", []):
                            last_id = upd["update_id"]
                            raw = (upd.get("message", {}).get("text") or "").strip()
                            txt = raw.lower()
                            cid = upd.get("message", {}).get("chat", {}).get("id")
                            if not txt:
                                continue

                            if txt in ("/start", "/status"):
                                coin_val = 0
                                pos_lines = []
                                for p in positions[:10]:
                                    lp = LIVE_TICKERS.get(p["pair"], {}).get("last") or _latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                    pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
                                    val = p["qty"] * lp
                                    coin_val += val
                                    pos_lines.append(f"{p['pair']} Rp{val:,.0f} ({pnl:+.2f}%)")
                                total_eq = _latest_balance + coin_val
                                sells_today = [t for t in get_trades_by_period("day") if t["side"] == "sell" and t["pnl"] is not None]
                                pnl_today = sum(t["pnl"] for t in sells_today)
                                pnl_tag = f" | Today: Rp{pnl_today:+,.0f}" if sells_today else ""
                                text = (
                                    f"🤖 FMA ALPHA QUANT LABS\n"
                                    f"Equity: Rp{total_eq:,.0f} | Cash: Rp{_latest_balance:,.0f}{pnl_tag}\n"
                                    f"Mode: {_latest_regime.get('regime','?')} | Posisi: {len(positions)}\n" +
                                    ("\n".join(pos_lines) if pos_lines else "Tidak ada posisi")
                                )
                                await _reply(cid, text)
                                continue

                            if txt in ("/commands", "/help"):
                                await _reply(cid, (
                                    "📋 PERINTAH\n\n"
                                    "── INFO ──\n"
                                    "/status — Portfolio & posisi\n"
                                    "/risk — Parameter risiko saat ini\n"
                                    "/cycle — Status siklus terakhir\n"
                                    "/why — Alasan bot gak entry\n"
                                    "\n"
                                    "── PERFORMANCE ──\n"
                                    "/today — Performa hari ini\n"
                                    "/week — Performa minggu ini\n"
                                    "/month — Performa bulan ini\n"
                                    "/year — Performa tahun ini\n"
                                    "/perf — Statistik lifetime\n"
                                    "/project — Proyeksi equity\n"
                                    "/log — Transaksi terakhir\n"
                                    "\n"
                                    "── TRADING ──\n"
                                    "/atr — ATR/SL/TP posisi aktif\n"
                                    "/atr <coin> — ATR spesifik koin\n"
                                    "/ask <coin> — Detail sinyal koin\n"
                                    "\n"
                                    "── LAINNYA ──\n"
                                    "/greed — Bypass daily loss (1×/hari)\n"
                                    "/help — Daftar ini"
                                ))
                                continue

                            if txt == "/cycle":
                                if _cycle_last_end == 0:
                                    text = "Cycle belum dimulai. Tunggu ~5 menit."
                                else:
                                    sec_since = int(time.time() - _cycle_last_end)
                                    next_in = max(0, config.LOOP_INTERVAL_SECONDS - sec_since)
                                    cinfo = _cycle_last_info
                                    text = (
                                        f"📊 SIKLUS TERAKHIR\n"
                                        f"Cycle #{cinfo.get('cycle', '?')}\n"
                                        f"{sec_since // 60}m {sec_since % 60}s lalu\n"
                                        f"⏳ Siklus berikutnya ~{next_in // 60}m {next_in % 60}s\n"
                                        f"──────────────\n"
                                        f"Regime: {cinfo.get('regime', '?')}\n"
                                        f"Cash: Rp{cinfo.get('cash', 0):,.0f}\n"
                                        f"Posisi: {cinfo.get('positions', 0)}\n"
                                        f"Durasi: {cinfo.get('duration', 0)}s"
                                    )
                                await _reply(cid, text)
                                continue

                            if txt == "/project":
                                eq = _latest_balance + sum(p["qty"] * (LIVE_TICKERS.get(p["pair"], {}).get("last") or _latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)) for p in positions)
                                curve = persist.load_equity_curve()
                                base_eq = curve[0] if curve else eq
                                pnl_pct_total = ((eq - base_eq) / base_eq) * 100 if base_eq else 0
                                sells = [t for t in get_trades_by_period("year") if t["side"] == "sell" and t["pnl"] is not None]
                                total_pnl = sum(t["pnl"] for t in sells)
                                days_running = max(1, len(set(t["timestamp"][:10] for t in sells))) if sells else 1
                                daily_avg = total_pnl / days_running if sells else 0
                                text = (
                                    f"📈 PROYEKSI\n"
                                    f"Equity: Rp{eq:,.0f} (dari Rp{base_eq:,.0f})\n"
                                    f"Perubahan: {pnl_pct_total:+.1f}%\n"
                                    f"Avg/hari: Rp{daily_avg:+,.0f}\n"
                                    f"30 hari: Rp{daily_avg * 30:+,.0f}\n"
                                    f"365 hari: Rp{daily_avg * 365:+,.0f}"
                                )
                                await _reply(cid, text)
                                continue

                            if txt in ("/today", "/week", "/month", "/year"):
                                period_map = {"/today": "day", "/week": "week", "/month": "month", "/year": "year"}
                                period = period_map[txt]
                                label_map = {"day": "HARI INI", "week": "MINGGU INI", "month": "BULAN INI", "year": "TAHUN INI"}
                                label = label_map[period]
                                try:
                                    if period == "week":
                                        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
                                        wk_start = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
                                        trades_in = get_trades_by_period("month")
                                        trades_in = [t for t in trades_in if t.get("timestamp", "").startswith(wk_start)]
                                    else:
                                        trades_in = get_trades_by_period(period)
                                    sells = [t for t in trades_in if t["side"] == "sell" and t["pnl"] is not None]
                                    buys = len([t for t in trades_in if t["side"] == "buy"])
                                    total_pnl = sum(t["pnl"] for t in sells)
                                    wins = [t for t in sells if t["pnl"] > 0]
                                    losses = [t for t in sells if t["pnl"] <= 0]
                                    fee_buy = buys * 25000 * config.TAKER_FEE_PCT
                                    fee_sell = sum(abs(t.get("amount_idr", 0) or 0) for t in sells) * config.MAKER_FEE_PCT
                                    text = (
                                        f"📊 {label}\n"
                                        f"Buy {buys}x | Sell {len(sells)}x ({len(wins)}W/{len(losses)}L)\n"
                                        f"Win rate: {len(wins)/max(len(sells),1)*100:.0f}%\n"
                                        f"PnL: Rp{total_pnl:+,.0f} | Fee: Rp{fee_buy+fee_sell:,.0f}\n"
                                        f"Net: Rp{total_pnl - fee_buy - fee_sell:+,.0f}"
                                    )
                                    if wins:
                                        best = max(wins, key=lambda x: x["pnl"])
                                        text += f"\nBest: {best.get('reason','?')} ({best['pnl']:+.0f})"
                                    if losses:
                                        worst = min(losses, key=lambda x: x["pnl"])
                                        text += f"\nWorst: {worst.get('reason','?')} ({worst['pnl']:+.0f})"
                                except Exception as e:
                                    text = f"Error: {str(e)[:60]}"
                                await _reply(cid, text)
                                continue

                            if txt == "/perf":
                                all_sells = get_trades_by_period("year")
                                sells = [t for t in all_sells if t["side"] == "sell" and t["pnl"] is not None]
                                wins = [t for t in sells if t["pnl"] > 0]
                                losses = [t for t in sells if t["pnl"] <= 0]
                                total = len(sells)
                                wr = (len(wins) / total * 100) if total > 0 else 0
                                total_pnl = sum(t["pnl"] for t in sells)
                                avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
                                avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
                                buf = f"📊 PERFORMANCE (year)\n"
                                buf += f"Sells: {total}x ({len(wins)}W/{len(losses)}L)\n"
                                buf += f"Win rate: {wr:.1f}%\n"
                                buf += f"Total PnL: Rp{total_pnl:+,.0f}\n"
                                if avg_win > 0 and avg_loss > 0:
                                    buf += f"R:R ratio: {avg_win/avg_loss:.2f}\n"
                                if sells:
                                    best = max(sells, key=lambda x: x["pnl"])
                                    worst = min(sells, key=lambda x: x["pnl"])
                                    buf += f"Best: {best.get('reason','?')} ({best['pnl']:+.0f})\n"
                                    buf += f"Worst: {worst.get('reason','?')} ({worst['pnl']:+.0f})"
                                await _reply(cid, buf)
                                continue

                            if txt == "/log":
                                all_recent = get_recent_trades(10)
                                buf = "📋 RIWAYAT TRANSAKSI\n"
                                if all_recent:
                                    for t in reversed(all_recent):
                                        ts = t.get("timestamp", "")[11:19] if t.get("timestamp") else "??:??"
                                        side = t.get("side", "?").upper()
                                        price = t.get("price", 0) or 0
                                        qty = t.get("qty", 0) or 0
                                        amount = t.get("amount_idr", 0) or 0
                                        pnl = t.get("pnl")
                                        raw_reason = t.get("reason", "") or ""
                                        parts = raw_reason.rsplit(" ", 1)
                                        reason_clean = parts[0][:30] if len(parts) > 1 else raw_reason[:30]
                                        pair_name = parts[-1] if len(parts) > 1 and "_idr" in parts[-1] else ""
                                        emoji = "🟢" if pnl and pnl > 0 else "🔴" if pnl and pnl <= 0 else "⚪"
                                        buf += f"{emoji} {ts} {side} {pair_name}\n"
                                        if side == "SELL" and pnl is not None:
                                            buf += f"   Rp{price:,.0f} × {qty:.4f} | PnL: {pnl:+,.0f} | {reason_clean}\n"
                                        else:
                                            buf += f"   Rp{price:,.0f} × {qty:.4f} (Rp{amount:,.0f}) | {reason_clean}\n"
                                elif _recent_actions:
                                    for a in reversed(_recent_actions[-10:]):
                                        t_str = f"{int((time.time()-a['time'])/60)}m" if a['time'] else "?"
                                        emoji = "🟢" if a.get("pnl", 0) > 0 else "🔴" if a.get("pnl", 0) < 0 else "⚪"
                                        buf += f"{emoji} [{t_str}] {a['action']} {a['pair']} ({a['pnl']:+.1f}%)\n"
                                else:
                                    buf += "Belum ada aktivitas."
                                await _reply(cid, buf)
                                continue

                            if txt == "/risk":
                                max_pos_runtime = config.ROTHSCHILD_OPEN_POSITIONS if config.ROTHSCHILD_ACTIVE else config.MAX_OPEN_POSITIONS
                                kelly_r = portfolio_risk.kelly_for_regime(_latest_regime.get("regime", "?"))
                                buf = "⚠️ RISK STATUS\n"
                                buf += f"Regime: {_latest_regime.get('regime', '?')}\n"
                                buf += f"Mode: {'🔴 ROTHSCHILD' if config.ROTHSCHILD_ACTIVE else '🟢 KONSERVATIF'}\n"
                                buf += f"Kelly: {kelly_r*100:.0f}%\n"
                                buf += f"ATR SL: {config.ATR_SL_MULTIPLIER:.1f}x | TP: {config.ATR_TP_MULTIPLIER:.1f}x\n"
                                buf += f"Max pos: {max_pos_runtime} | Max trade/hari: {config.MAX_DAILY_TRADES}\n"
                                buf += f"Daily loss: Rp{config.DAILY_LOSS_FLOOR_IDR:,} | Drawdown: {abs(config.PORTFOLIO_STOP_LOSS_PCT)*100:.0f}%\n"
                                for p in positions[:5]:
                                    last_p = LIVE_TICKERS.get(p["pair"], {}).get("last") or _latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                    pnl_r = pnl_pct(p.get("entry_price") or 0, last_p, p["side"])
                                    flag = "⚠️" if pnl_r < -5 else "✅"
                                    buf += f"{flag} {p['pair']} ({pnl_r:+.1f}%)\n"
                                await _reply(cid, buf)
                                continue

                            if txt.startswith("/ask "):
                                coin = txt.split("/ask ", 1)[1].strip().upper()
                                detail = await _build_coin_detail(coin)
                                await _reply(cid, detail)
                                continue

                            if txt == "/greed":
                                global _daily_loss_hit_today, _greed_used_today
                                _greed_used_today = True
                                _daily_loss_hit_today = False
                                risk.today_peak = _latest_balance + sum(p["qty"] * (LIVE_TICKERS.get(p["pair"], {}).get("last") or p.get("entry_price", 0)) for p in positions)
                                persist.save_daily_loss_hit(False)
                                persist.save_today_peak(risk.today_peak)
                                await send_message("🟢 GREED MODE — peak reset, bot jalan normal sampe midnight.")
                                continue

                            if txt == "/why":
                                regime_r = _latest_regime.get("regime", "")
                                reason = f"Regime: {regime_r}"
                                if regime_r in ("BEAR", "HIGH_VOL"):
                                    reason += f" — bot tidak entry di {regime_r}"
                                elif regime_r == "SIDEWAYS":
                                    reason += " — mean-reversion, nunggu oversold"
                                elif regime_r == "BULL":
                                    reason += " — momentum, nunggu sinyal"
                                if _latest_balance < config.MIN_ORDER_IDR:
                                    reason += f"\nCash Rp{_latest_balance:,.0f} < min Rp{config.MIN_ORDER_IDR:,}"
                                if not _latest_ohlcv_map_1h:
                                    reason += "\nData OHLCV kosong"
                                if not positions:
                                    reason += "\nTidak ada posisi"
                                else:
                                    for p in positions[:5]:
                                        lp = LIVE_TICKERS.get(p["pair"], {}).get("last") or _latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                        pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
                                        atr_for = risk.compute_atr(_latest_ohlcv_map_1h.get(p["pair"], []))
                                        sm_state = _position_states.get(p["pair"], {}).get("state", "?")
                                        reason += f"\n{p['pair']} ({pnl:+.1f}%) SM:{sm_state}"
                                await _reply(cid, reason)
                                continue

                            if txt.startswith("/atr"):
                                coin_arg = txt.replace("/atr", "").strip().upper()
                                pairs_to_check = []
                                if coin_arg:
                                    pid = coin_arg if coin_arg.endswith("_IDR") else f"{coin_arg}_IDR"
                                    pairs_to_check = [pid.lower()]
                                else:
                                    pairs_to_check = [p["pair"] for p in positions]
                                if not pairs_to_check:
                                    await _reply(cid, "Tidak ada posisi. Gunakan: /atr <coin>")
                                    continue
                                atr_lines = []
                                for pid in pairs_to_check[:8]:
                                    try:
                                        ohlcv = _latest_ohlcv_map_1h.get(pid, [])
                                        if len(ohlcv) < 15:
                                            atr_lines.append(f"{pid}: data OHLCV kurang")
                                            continue
                                        atr_val = risk.compute_atr(ohlcv)
                                        price = float(ohlcv[-1]["close"])
                                        p = next((x for x in positions if x["pair"] == pid), None)
                                        entry = p["entry_price"] if p else price
                                        side = p["side"] if p else "BUY"
                                        sl, tp = risk.get_sl_tp(entry, side, atr_val)
                                        pnl = pnl_pct(entry, price, side) if p else 0
                                        sl_pct = (sl - entry) / entry * 100
                                        tp_pct = (tp - entry) / entry * 100
                                        tag = f" ({pnl:+.1f}%)" if p else ""
                                        atr_lines.append(f"{pid:15} Rp{price:>8,.0f} | ATR {atr_val:.1f}% | SL Rp{sl:,.0f} ({sl_pct:+.1f}%) | TP Rp{tp:,.0f} ({tp_pct:+.1f}%){tag}")
                                    except Exception as e:
                                        atr_lines.append(f"{pid}: error ({str(e)[:30]})")
                                await _reply(cid, "-- ATR Levels --\n" + "\n".join(atr_lines))
                                continue

                            if txt.startswith("/"):
                                await _reply(cid, "Perintah tidak dikenal. Ketik /help untuk daftar lengkap.")
                                continue
            except Exception:
                pass
            await asyncio.sleep(5)

    set_on_tick(_realtime_sltp_check)
    ws_task = asyncio.create_task(market_ws_loop())
    pws_task = asyncio.create_task(private_ws_loop())
    momentum_task = asyncio.create_task(_momentum_scanner())
    opt_task = asyncio.create_task(_optimizer_loop())
    for _ in range(6):
        await asyncio.sleep(0.5)


    async with httpx.AsyncClient(timeout=30) as client:
        poller = asyncio.create_task(telegram_poller())
        bal_poller = asyncio.create_task(_balance_poller(client))

        while not shutdown_flag:
            print(f"\n{'='*20} FMA ALPHA QUANT LABS — Cycle #{cycle_counter + 1} {'='*20}", flush=True)
            await portfolio_cycle(client)
            for _ in range(config.LOOP_INTERVAL_SECONDS // 5):
                if shutdown_flag:
                    break
                await asyncio.sleep(5)

    poller.cancel()
    bal_poller.cancel()
    mws_stop()
    pws_stop()
    if config.INDODAX_API_KEY:
        try:
            async with httpx.AsyncClient() as _dc:
                await cancel_deadman(_dc)
        except Exception:
            pass
    if _tp_limit_orders and config.INDODAX_API_KEY:
        try:
            for pid, oid in list(_tp_limit_orders.items()):
                cancel_params = {
                    "method": "cancelOrder", "timestamp": int(time.time() * 1000),
                    "recvWindow": "5000", "pair": pid,
                    "order_id": str(oid), "type": "sell",
                }
                cancel_body = urlencode(cancel_params)
                cancel_sig = hmac.new(config.INDODAX_SECRET_KEY.encode(), cancel_body.encode(), hashlib.sha512).hexdigest()
                async with httpx.AsyncClient() as _tc:
                    await _tc.post(config.INDODAX_TAPI_URL, headers={
                        "Key": config.INDODAX_API_KEY, "Sign": cancel_sig,
                        "Content-Type": "application/x-www-form-urlencoded",
                    }, content=cancel_body)
        except Exception:
            pass
    if _position_states:
        try:
            async with httpx.AsyncClient() as _sc:
                for pid, sm in list(_position_states.items()):
                    oid = sm.get("tp_order_id") or sm.get("sl_order_id")
                    if oid:
                        await _sm_cancel(_sc, oid, pid)
        except Exception:
            pass
    print("Shutdown complete.", flush=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    except Exception as e:
        print(f"Fatal: {e}", flush=True)
        import traceback
        traceback.print_exc()
