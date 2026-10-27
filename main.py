# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import asyncio
import datetime
import os
import signal
import time

import httpx

import config
import persist
import patterns
import rules
from data_layer import fetch_ohlcv, fetch_orderbook
from deadman import cancel_deadman
from db import (init_db, get_recent_trades, get_recent_completed_sells,
                get_trades_by_period, count_new_completed_sells, get_max_trade_id,
                init_chat_db)
from executor import place_order, get_balance, cancel_order, get_open_orders
from market_ws import market_ws_loop, LIVE_TICKERS, stop as mws_stop, set_on_tick
from notifier import send_message
from orders import registry
from private_ws import private_ws_loop, stop as pws_stop, set_on_fill
from state_bus import TICK
from engine import state as S
from engine.cycle import portfolio_cycle
from engine.position_sm import (
    pnl_pct, add_position, _handle_fill_event, _realtime_sltp_check, _sm_cancel,
)


def _on_ws_tick(pair: str, price: float) -> None:
    """Dipanggil sinkron dari task market_ws: cuma nitip pesan, nol await,
    nol sentuhan state."""
    S.state_bus.publish_tick(pair, price)


def _on_ws_fill(symbol: str, side: str, status: str) -> None:
    """Dipanggil sinkron dari task private_ws."""
    pair = symbol.replace("idr", "_idr") if not symbol.endswith("_idr") else symbol
    S.state_bus.publish_fill(pair, side, status)


async def _state_consumer():
    """Satu-satunya jalan event WS menjadi mutasi state, serial dengan cycle."""
    while not S.shutdown_flag:
        await S.state_bus.wait()
        if S.shutdown_flag:
            return
        for ev in S.state_bus.drain():
            if S.shutdown_flag:
                return
            async with S._state_lock:
                try:
                    if ev.kind == TICK:
                        await _realtime_sltp_check(ev.pair, ev.price)
                    else:
                        await _handle_fill_event(ev.pair, ev.side, ev.status)
                except Exception as e:
                    print(f"  WS event error {ev.kind} {ev.pair}: {e}", flush=True)


def handle_sig(*_):
    S.shutdown_flag = True


async def _optimizer_loop():
    _warned_no_key = False
    while not S.shutdown_flag:
        await asyncio.sleep(15)
        if not config.DEEPSEEK_API_KEY:
            if not _warned_no_key and S.cycle_counter > 0:
                _warned_no_key = True
                await send_message("⚠️ AI OPTIMIZER: DeepSeek API key tidak terisi/balance habis — optimasi nonaktif. Bot tetap jalan dengan parameter terakhir.")
            continue
        if S.cycle_counter == 0:
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
            msg = await S.optimizer.run(recent_sells, eq_curve, S.regime_history)
            if msg:
                await send_message(msg)
            latest_id = get_max_trade_id()
            persist.save_optimizer_state({"last_trade_id": latest_id, "last_run_time": time.time()})
        except Exception as opt_e:
            print(f"  AI Optimizer error: {opt_e}", flush=True)

async def _balance_poller(client: httpx.AsyncClient):
    while not S.shutdown_flag:
        try:
            info = await get_balance(client)
            S._latest_balance = float(info.get("balance", {}).get("idr", 0))
        except Exception as e:
            print(f"Balance poller error: {e}", flush=True)
        await asyncio.sleep(30)

async def _momentum_scanner():
    _last_scan_pairs: dict[str, float] = {}
    while not S.shutdown_flag:
        try:
            await asyncio.sleep(config.MOMENTUM_SCAN_INTERVAL)
            if not S._latest_ohlcv_map_1h:
                continue
            if S._daily_loss_hit_today:
                print(f"  Momentum scanner: daily loss hit — skip entry", flush=True)
                continue
            regime_now = S._latest_regime.get("regime", "")
            if regime_now in ("BEAR", "HIGH_VOL"):
                print(f"  Momentum scanner: {regime_now} — skip entry", flush=True)
                continue
            from db import get_trade_count_today
            if get_trade_count_today() >= config.MAX_DAILY_TRADES:
                print(f"  Momentum scanner: max trades/day ({config.MAX_DAILY_TRADES}) — skip", flush=True)
                continue
            cash_avail = S._latest_balance
            if cash_avail < config.MIN_ORDER_IDR:
                continue
            max_pos = config.MAX_OPEN_POSITIONS if not S._rothschild_active else config.ROTHSCHILD_OPEN_POSITIONS
            if len(S.positions) >= max_pos:
                continue
            for pid, ohlcv in list(S._latest_ohlcv_map_1h.items()):
                if len(S.positions) >= max_pos:
                    break
                if any(p["pair"] == pid for p in S.positions):
                    continue
                if pid in config.STABLECOINS or pid in config.SKIP_COINS or pid in rules.DCA_EXCLUSIVE:
                    continue
                if pid in S._coin_blacklist or pid in S._realtime_sold:
                    continue
                if pid in S._cooldown and time.time() < S._cooldown[pid]:
                    continue
                if time.time() - _last_scan_pairs.get(pid, 0) < 30:
                    continue
                price = LIVE_TICKERS.get(pid, {}).get("last") or S._latest_ticker_map.get(pid, {}).get("last", 0)
                if not price or price < 20:
                    continue
                signal = S.momentum_engine.evaluate(pid, ohlcv, price)
                if not signal:
                    continue
                _last_scan_pairs[pid] = time.time()
                print(f"  MOMENTUM: {pid} {signal}", flush=True)
                atr_chk = S.risk.compute_atr(ohlcv, clamped=False)
                if atr_chk < 0.5:
                    print(f"    ATR {atr_chk:.1f}% < 1.5 — skip (terlalu stabil)", flush=True)
                    continue
                if atr_chk > 25.0:
                    print(f"    ATR {atr_chk:.1f}% > 25 — skip", flush=True)
                    continue
                vol_idr = float(S._latest_ticker_map.get(pid, {}).get("vol_idr", 0) or 0)
                if vol_idr < 500_000_000:
                    print(f"    Vol Rp{vol_idr:,.0f} < 500M — skip", flush=True)
                    continue
                if len(ohlcv) >= 15:
                    pcloses = [float(c["close"]) for c in ohlcv[-30:]]
                    pat = patterns.detect_paradiddle(pcloses)
                    if pat in ("FAKE_BREAKOUT_SELL", "EXHAUSTION_SELL"):
                        print(f"    Pattern {pat} — skip", flush=True)
                        continue
                if pid in S._pair_meta:
                    try:
                        async with httpx.AsyncClient() as _obc:
                            _ob = await fetch_orderbook(_obc, pair=pid, depth=3)
                        if _ob and _ob.get("imbalance_pct", 0) < -10:
                            print(f"    Book sell pressure {_ob['imbalance_pct']:.0f}% — skip", flush=True)
                            continue
                    except Exception:
                        pass
                if pid in S._sm_cooldown and S._sm_cooldown[pid] > time.time():
                    print(f"    Cooldown: {pid} — skip", flush=True)
                    continue
                sig_m = S._latest_all_signals.get(pid, {})
                m_score = 0
                rsi_m = sig_m.get("rsi")
                if rsi_m is not None and rsi_m < 55: m_score += 1
                vr_m = sig_m.get("volume_ratio")
                if vr_m is not None and 0.5 <= vr_m <= 3.0: m_score += 1
                atr_m = sig_m.get("atr_pct")
                if atr_m is not None and atr_m < 8: m_score += 1
                streak_m = sig_m.get("momentum_streak")
                if streak_m is not None and abs(streak_m) < 3: m_score += 1
                if m_score < 3:
                    print(f"    Entry score: {pid} s{m_score}/4 — skip (kualitas rendah)", flush=True)
                    continue
                print(f"    Entry score: {pid} s{m_score}/4 — OK", flush=True)
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
                if not S.risk.is_profit_viable(price, qty, "BUY", atr_pct=atr_chk):
                    print(f"    Fee makan profit — skip", flush=True)
                    continue
                try:
                    async with S._state_lock:
                        async with httpx.AsyncClient() as _mc:
                            order = await place_order(_mc, "buy", price, amount, pair=pid, order_type="market")
                        if order.get("order_id") or order.get("receive_rp"):
                            coin_name = pid.split("_")[0]
                            actual_qty = float(order.get(f"receive_{coin_name}", 0)) or qty
                            actual_spend = float(order.get("spend_rp", 0)) or amount
                            add_position(S.positions, pid, "BUY", actual_spend / actual_qty if actual_qty else price,
                                         actual_qty, actual_spend, None, time.time(),
                                         "ROTHSCHILD" if S._rothschild_active else "KONSERVATIF")
                            persist.save_positions(S.positions)
                            S._ext_entry_prices[pid] = actual_spend / actual_qty if actual_qty else price
                            persist.save_entry_prices(S._ext_entry_prices)
                            cash_avail -= actual_spend
                            await send_message(f"⚡ MOMENTUM {signal}: BUY {pid}\nRp{actual_spend:,.0f} @ {price:,.0f}")
                            S._momentum_entry_time[pid] = time.time()
                            print(f"    EXECUTED: BUY {pid}", flush=True)
                except Exception as ex:
                    print(f"    Order failed: {ex}", flush=True)
        except Exception as e:
            print(f"Momentum scanner error: {e}", flush=True)


async def main():

    if config.HAS_TRADING_KEYS:
        try:
            async with httpx.AsyncClient() as _bc:
                info = await get_balance(_bc)
                S._latest_balance = float(info.get("balance", {}).get("idr", 0))
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
            S.positions.extend(saved)
        saved_tp = persist.load_today_peak()
        if saved_tp > S.risk.today_peak and 1_000 < saved_tp < 100_000_000_000:
            S.risk.today_peak = saved_tp
            print(f"  Restored today_peak from persist: Rp{S.risk.today_peak:,.0f}", flush=True)
        print("DB init OK", flush=True)
        if os.getenv("CLEAR_LOSS_HOLD"):
            persist.save_daily_loss_hit(False)
            persist.save_loss_hit_date("")
            _daily_loss_hit_today = False
            print(f"  CLEAR_LOSS_HOLD: daily loss flag reset by env", flush=True)
        _daily_loss_hit_today = persist.load_daily_loss_hit()
        if _daily_loss_hit_today:
            print(f"  Previous session hit daily loss — TP allow, SL hold", flush=True)
        S._ext_entry_prices.update(persist.load_entry_prices())
        print(f"Loaded {len(S._ext_entry_prices)} entry prices from DB", flush=True)
        recent = get_recent_trades(limit=100)
        S.portfolio_risk.set_trade_history(recent)
        print(f"Kelly: {len(recent)} trades loaded, optimal f={S.portfolio_risk.kelly.optimal_fraction():.2f}", flush=True)
        saved_blacklist = persist.load_blacklist()
        if saved_blacklist:
            S._coin_blacklist.update(saved_blacklist)
            print(f"Restored {len(S._coin_blacklist)} blacklisted pairs", flush=True)
        saved_cooldown = persist.load_cooldown()
        if saved_cooldown:
            now = time.time()
            S._cooldown.update({k: v for k, v in saved_cooldown.items() if v > now})
            print(f"Restored {len(S._cooldown)} active cooldowns", flush=True)
        saved_sm_cd = persist.load_sm_cooldown()
        if saved_sm_cd:
            now = time.time()
            S._sm_cooldown.update({k: v for k, v in saved_sm_cd.items() if v > now})
            print(f"Restored {len(S._sm_cooldown)} active SM cooldowns", flush=True)
        S.grid_mini.load_instances()
        if S.grid_mini.instances:
            print(f"Restored {len(S.grid_mini.instances)} grid instances", flush=True)
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

    if config.HAS_TRADING_KEYS and not config.PAPER_TRADING:
        try:
            async with httpx.AsyncClient(timeout=10) as _cc:
                orders_data = await get_open_orders(_cc)
                orders_by_pair = orders_data if isinstance(orders_data, dict) else {}
                cancel_count = 0
                for opair, olist in orders_by_pair.items():
                    if isinstance(olist, list):
                        for o in olist:
                            oid = o.get("order_id")
                            if not oid:
                                continue
                            await cancel_order(_cc, oid, pair=opair, side=o.get("type", "buy"))
                            cancel_count += 1
                if cancel_count:
                    print(f"CLEANUP: cancelled {cancel_count} stale orders — fresh start for SM & Grid", flush=True)
        except Exception as e:
            print(f"Order cleanup: {e}", flush=True)

    mode_n = "INSANE 🚀" if config.INSANE_MODE else ("ALPHA 🔴" if config.ALPHA_MODE else "STANDARD")
    tapi_n = "TAPI v2" if config.USE_TAPI_V2 else "TAPI v1 (legacy)"
    if config.USE_TAPI_V2 and not (config.INDODAX_API_KEY and config.INDODAX_SECRET_KEY):
        print("  WARN: no legacy key — deadman switch is OFF (v2 has no countdownCancelAll)", flush=True)
    ok = await send_message(
        f"🤖 FMA ALPHA QUANT LABS started (Engineering Mode)\n"
        f"Target: Rp410k → Rp1jt 🚀\n"
        f"Rules Engine — top {config.MAX_SCAN_PAIRS} pairs by volume\n"
        f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'} | {mode_n} | {tapi_n}\n"
        f"SL ATR×{config.ATR_SL_MULTIPLIER:.0f} | TP ATR×{config.ATR_TP_MULTIPLIER:.0f}\n"
        f"Notifikasi hanya event-based (no spam tiap 5 menit)"
    )
    print(f"Telegram: {'OK' if ok else 'FAILED'}", flush=True)

    async def _build_coin_detail(pair: str) -> str:
        pid = (pair if pair.endswith("_idr") else f"{pair}_idr").lower()
        sig = S._latest_all_signals.get(pid, {})
        t = S._latest_ticker_map.get(pid, {})
        lt = LIVE_TICKERS.get(pid, {})
        p = next((x for x in S.positions if x["pair"] == pid), None)
        ohlcv = S._latest_ohlcv_map_1h.get(pid, [])
        atr_val = S.risk.compute_atr(ohlcv) if ohlcv else None
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
        last_id = 0
        while not S.shutdown_flag:
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
                                for p in S.positions[:10]:
                                    lp = LIVE_TICKERS.get(p["pair"], {}).get("last") or S._latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                    pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
                                    val = p["qty"] * lp
                                    coin_val += val
                                    pos_lines.append(f"{p['pair']} Rp{val:,.0f} ({pnl:+.2f}%)")
                                total_eq = S._latest_balance + coin_val
                                sells_today = [t for t in get_trades_by_period("day") if t["side"] == "sell" and t["pnl"] is not None]
                                pnl_today = sum(t["pnl"] for t in sells_today)
                                pnl_tag = f" | Today: Rp{pnl_today:+,.0f}" if sells_today else ""
                                text = (
                                    f"🤖 FMA ALPHA QUANT LABS\n"
                                    f"Equity: Rp{total_eq:,.0f} | Cash: Rp{S._latest_balance:,.0f}{pnl_tag}\n"
                                    f"Mode: {S._latest_regime.get('regime','?')} | Posisi: {len(S.positions)}\n" +
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
                                if S._cycle_last_end == 0:
                                    text = "Cycle belum dimulai. Tunggu ~5 menit."
                                else:
                                    sec_since = int(time.time() - S._cycle_last_end)
                                    next_in = max(0, config.LOOP_INTERVAL_SECONDS - sec_since)
                                    cinfo = S._cycle_last_info
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
                                eq = S._latest_balance + sum(p["qty"] * (LIVE_TICKERS.get(p["pair"], {}).get("last") or S._latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)) for p in S.positions)
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
                                elif S._recent_actions:
                                    for a in reversed(S._recent_actions[-10:]):
                                        t_str = f"{int((time.time()-a['time'])/60)}m" if a['time'] else "?"
                                        emoji = "🟢" if a.get("pnl", 0) > 0 else "🔴" if a.get("pnl", 0) < 0 else "⚪"
                                        buf += f"{emoji} [{t_str}] {a['action']} {a['pair']} ({a['pnl']:+.1f}%)\n"
                                else:
                                    buf += "Belum ada aktivitas."
                                await _reply(cid, buf)
                                continue

                            if txt == "/risk":
                                max_pos_runtime = config.ROTHSCHILD_OPEN_POSITIONS if config.ROTHSCHILD_ACTIVE else config.MAX_OPEN_POSITIONS
                                kelly_r = S.portfolio_risk.kelly_for_regime(S._latest_regime.get("regime", "?"))
                                buf = "⚠️ RISK STATUS\n"
                                buf += f"Regime: {S._latest_regime.get('regime', '?')}\n"
                                buf += f"Mode: {'🔴 ROTHSCHILD' if config.ROTHSCHILD_ACTIVE else '🟢 KONSERVATIF'}\n"
                                buf += f"Kelly: {kelly_r*100:.0f}%\n"
                                buf += f"ATR SL: {config.ATR_SL_MULTIPLIER:.1f}x | TP: {config.ATR_TP_MULTIPLIER:.1f}x\n"
                                buf += f"Max pos: {max_pos_runtime} | Max trade/hari: {config.MAX_DAILY_TRADES}\n"
                                buf += f"Daily loss: Rp{config.DAILY_LOSS_FLOOR_IDR:,} | Drawdown: {abs(config.PORTFOLIO_STOP_LOSS_PCT)*100:.0f}%\n"
                                for p in S.positions[:5]:
                                    last_p = LIVE_TICKERS.get(p["pair"], {}).get("last") or S._latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
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
                                S._greed_used_today = True
                                S._daily_loss_hit_today = False
                                S.risk.today_peak = S._latest_balance + sum(p["qty"] * (LIVE_TICKERS.get(p["pair"], {}).get("last") or p.get("entry_price", 0)) for p in S.positions)
                                persist.save_daily_loss_hit(False)
                                persist.save_today_peak(S.risk.today_peak)
                                await send_message("🟢 GREED MODE — peak reset, bot jalan normal sampe midnight.")
                                continue

                            if txt == "/why":
                                regime_r = S._latest_regime.get("regime", "")
                                reason = f"Regime: {regime_r}"
                                if regime_r in ("BEAR", "HIGH_VOL"):
                                    reason += f" — bot tidak entry di {regime_r}"
                                elif regime_r == "SIDEWAYS":
                                    reason += " — mean-reversion, nunggu oversold"
                                elif regime_r == "BULL":
                                    reason += " — momentum, nunggu sinyal"
                                if S._latest_balance < config.MIN_ORDER_IDR:
                                    reason += f"\nCash Rp{S._latest_balance:,.0f} < min Rp{config.MIN_ORDER_IDR:,}"
                                if not S._latest_ohlcv_map_1h:
                                    reason += "\nData OHLCV kosong"
                                if not S.positions:
                                    reason += "\nTidak ada posisi"
                                else:
                                    for p in S.positions[:5]:
                                        lp = LIVE_TICKERS.get(p["pair"], {}).get("last") or S._latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                        pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
                                        atr_for = S.risk.compute_atr(S._latest_ohlcv_map_1h.get(p["pair"], []))
                                        sm_state = S._position_states.get(p["pair"], {}).get("state", "?")
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
                                    pairs_to_check = [p["pair"] for p in S.positions]
                                if not pairs_to_check:
                                    await _reply(cid, "Tidak ada posisi. Gunakan: /atr <coin>")
                                    continue
                                atr_lines = []
                                for pid in pairs_to_check[:8]:
                                    try:
                                        ohlcv = S._latest_ohlcv_map_1h.get(pid, [])
                                        if len(ohlcv) < 15:
                                            async with httpx.AsyncClient() as _ac:
                                                ohlcv = await fetch_ohlcv(_ac, pair=pid, tf=60, limit=50)
                                        if len(ohlcv) < 15:
                                            atr_lines.append(f"{pid}: data OHLCV kurang")
                                            continue
                                        atr_val = S.risk.compute_atr(ohlcv)
                                        price = float(ohlcv[-1]["close"])
                                        p = next((x for x in S.positions if x["pair"] == pid), None)
                                        entry = p["entry_price"] if p else price
                                        side = p["side"] if p else "BUY"
                                        sl, tp = S.risk.get_sl_tp(entry, side, atr_val)
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

    S.dca_smart.load_instances()
    if S.dca_smart.instances:
        print(f"Restored {len(S.dca_smart.instances)} DCA instances", flush=True)
    else:
        print("DCA Smart: starting fresh (no saved instances)", flush=True)

    async def _dca_task():
        _dca_heartbeat = 0
        print("  DCA task started", flush=True)
        async with httpx.AsyncClient(timeout=30) as _dc:
            while not S.shutdown_flag:
                try:
                    async with S._state_lock:
                        await S.dca_smart.run_cycle(_dc)
                except Exception as e:
                    print(f"DCA error: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
                _dca_heartbeat += 1
                if _dca_heartbeat % 20 == 0:
                    print(f"  DCA alive: {len(S.dca_smart.instances)} instances, balance Rp{S.dca_smart.balance_idr:,.0f}", flush=True)
                for _ in range(15):
                    if S.shutdown_flag:
                        break
                    await asyncio.sleep(1)

    set_on_tick(_on_ws_tick)
    set_on_fill(_on_ws_fill)
    ws_task = asyncio.create_task(market_ws_loop())
    pws_task = asyncio.create_task(private_ws_loop())
    state_task = asyncio.create_task(_state_consumer())
    momentum_task = asyncio.create_task(_momentum_scanner())
    opt_task = asyncio.create_task(_optimizer_loop())
    dca_task = asyncio.create_task(_dca_task())
    for _ in range(6):
        await asyncio.sleep(0.5)


    async with httpx.AsyncClient(timeout=30) as client:
        poller = asyncio.create_task(telegram_poller())
        bal_poller = asyncio.create_task(_balance_poller(client))

        while not S.shutdown_flag:
            print(f"\n{'='*20} FMA ALPHA QUANT LABS — Cycle #{S.cycle_counter + 1} {'='*20}", flush=True)
            async with S._state_lock:
                await portfolio_cycle(client)
            for _ in range(config.LOOP_INTERVAL_SECONDS // 5):
                if S.shutdown_flag:
                    break
                await asyncio.sleep(5)

    poller.cancel()
    bal_poller.cancel()
    mws_stop()
    pws_stop()
    state_task.cancel()
    if config.HAS_TRADING_KEYS:
        try:
            async with httpx.AsyncClient() as _dc:
                await cancel_deadman(_dc)
        except Exception:
            pass
    if registry and config.HAS_TRADING_KEYS:
        try:
            async with httpx.AsyncClient() as _tc:
                for t in registry.list():
                    await cancel_order(_tc, t.order_id, pair=t.pair, side=t.side)
                    registry.pop(t.pair, t.kind)
        except Exception:
            pass
    if S._position_states:
        try:
            async with httpx.AsyncClient() as _sc:
                for pid, sm in list(S._position_states.items()):
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
