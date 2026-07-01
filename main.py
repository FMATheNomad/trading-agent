import asyncio
import hashlib
import hmac
import sys
import signal
import time
from urllib.parse import urlencode
import httpx
import config
from data_layer import fetch_viable_pairs, fetch_ticker, fetch_ohlcv, fetch_ohlcv_both, fetch_all_tickers, fetch_orderbook
from indicators import compute_signals, compute_batch_signals, apply_ml_boost
from llm_filter import evaluate_portfolio
from cointegration import CointegrationEngine
from hmm_regime import HMMRegimeDetector
from ml_signal import XGBoostSignal
from features import MicrostructureFeatures
from risk_manager import RiskManager, PortfolioRiskManager
from executor import place_order, get_balance, get_order
from deadman import refresh_deadman, cancel_deadman
from notifier import send_message
from db import init_db, log_trade, log_decision, get_recent_trades, get_trade_count_today, save_chat, get_chat_history, init_chat_db
import persist
from market_ws import market_ws_loop, LIVE_TICKERS, stop as mws_stop
from private_ws import private_ws_loop, stop as pws_stop

risk = RiskManager()
portfolio_risk = PortfolioRiskManager()
hmm_detector = HMMRegimeDetector(n_states=config.HMM_N_STATES)
coint_engine = CointegrationEngine(z_entry=config.COINT_Z_ENTRY, z_exit=config.COINT_Z_EXIT)
xgboost_signal = XGBoostSignal()
positions: list[dict] = []
shutdown_flag = False

regime_history: list[str] = []
known_pairs: set[str] = set()
_ext_entry_prices: dict[str, float] = {}
_pair_meta: dict[str, dict] = {}
_hmm_trained_cycle = 0
_prev_regime: str = ""
_prev_equity: float = 0
_prev_signal_count: int = 0
_report_sent_count: int = 0
_coin_blacklist: set[str] = set()
_cio_stats: dict = {"total_decisions": 0, "buys": 0, "sells": 0, "wins": 0, "losses": 0}
_tp_limit_orders: dict[str, int] = {}
_pending_orders: dict[str, dict] = {}
_cooldown: dict[str, float] = {}
_latest_regime: dict = {}
_latest_ticker_map: dict = {}
_latest_all_signals: dict = {}
_latest_ohlcv_map_1h: dict = {}
_last_actual_balance: float = 0
_order_error_cooldown: dict[str, float] = {}

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

    hmm_result = hmm_detector.predict(ohlcv_map_1h) if ohlcv_map_1h else {}
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

def compute_pairs_suggestions(all_signals: dict, ticker_map: dict) -> list[dict]:
    suggestions = []
    for a, b in config.CORRELATION_PAIRS:
        ta = ticker_map.get(a, {}).get("last", 0)
        tb = ticker_map.get(b, {}).get("last", 0)
        if not ta or not tb:
            continue
        ratio = ta / tb
        suggestions.append({
            "pair": f"{a}/{b}", "ratio": round(ratio, 4),
            "a_price": ta, "b_price": tb,
        })
    return suggestions

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

cycle_counter = 0

async def portfolio_cycle(client: httpx.AsyncClient):
    global positions, cycle_counter, _prev_regime, _prev_equity, _prev_signal_count, _report_sent_count
    cycle_counter += 1
    _t0 = time.time()

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
                positions.append({
                    "pair": pid, "side": po.get("side", "BUY"),
                    "entry_price": po["price"], "qty": po["qty"],
                    "amount_idr": po["amount_idr"],
                    "atr_pct": po.get("atr_pct"), "entry_time": time.time(),
                })
                persist.save_positions(positions)
                print(f"  MAKER FILLED: {pid} → position opened", flush=True)
                del _pending_orders[pid]
                continue
            if status in ("cancelled", "rejected") or remain == 0:
                del _pending_orders[pid]
                continue
            if po.get("is_maker"):
                po["cycles"] = po.get("cycles", 0) + 1
            if po.get("cycles", 0) >= 2 and po.get("is_maker"):
                print(f"  MAKER TIMEOUT: {pid} → cancelling and retrying market", flush=True)
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
                except Exception:
                    pass
                del _pending_orders[pid]
        except Exception:
            del _pending_orders[pid]

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
                "min_base": int(v.get("trade_min_base_currency", config.MIN_ORDER_IDR)),
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
                    if len(o1) >= 30:
                        ohlcv_map_1h[pid] = o1
                        ticker_map[pid] = v["ticker"]
                    if len(o4) >= 30:
                        ohlcv_map_4h[pid] = o4
                except Exception as e:
                    print(f"  {pid}: {e}", flush=True)

        await asyncio.gather(*[fetch_one(v) for v in viable])

        for p in list(positions):
            pm = _pair_meta.get(p["pair"])
            if pm:
                val = p["qty"] * ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
                if val < pm["min_base"]:
                    print(f"  CLEANUP: {p['pair']} dust Rp{val:,.0f} < min Rp{pm['min_base']:,} — hapus", flush=True)
                    positions.remove(p)
                    persist.save_positions(positions)

        print(f"Computing signals: {len(ohlcv_map_1h)} pairs (1h) + {len(ohlcv_map_4h)} (4h)...", flush=True)
        all_signals = compute_batch_signals(ohlcv_map_1h, ohlcv_map_4h)

        for pid, ohlcv_1h in ohlcv_map_1h.items():
            if xgboost_signal.trained and pid in all_signals:
                ml_pred = xgboost_signal.predict(ohlcv_1h)
                all_signals[pid] = apply_ml_boost(all_signals[pid], ml_pred)

        pair_signals = coint_engine.scan(ohlcv_map_1h, config.CORRELATION_PAIRS) if ohlcv_map_1h else []
        active = [p for p in pair_signals if p["signal"] in ("SHORT_SPREAD", "LONG_SPREAD")]
        if active:
            for ps in active:
                print(f"COINT SIGNAL: {ps['pair']} → {ps['signal']} (z={ps['z_score']}, hl={ps.get('half_life_hours', '?')}h, H={ps.get('hurst', '?')})", flush=True)
        elif pair_signals:
            for ps in pair_signals[:3]:
                print(f"Coint: {ps['pair']} z={ps['z_score']} coint={ps.get('cointegrated', False)} hl={ps.get('half_life_hours', '?')}h", flush=True)

        regime_info = classify_regime(all_signals, ohlcv_map_1h)
        regime_history.append(regime_info["regime"])
        if len(regime_history) > 12:
            regime_history.pop(0)
        hmm_tag = f" HMM:{regime_info.get('hmm_regime', '?')}({regime_info.get('hmm_confidence', 0):.2f})" if regime_info.get('hmm_confidence', 0) > 0 else ""
        print(f"Regime: {regime_info['regime']}{hmm_tag} | B:{regime_info['buy_ratio']} S:{regime_info['sell_ratio']} "
              f"Score:{regime_info['avg_score']} HC:{regime_info['high_conviction_count']}", flush=True)

        if (not xgboost_signal.trained or cycle_counter % 50 == 0) and len(ohlcv_map_1h) >= config.ML_TRAIN_MIN_SAMPLES:
            try:
                xgboost_signal.train(ohlcv_map_1h)
                if xgboost_signal.trained:
                    print("XGBoost signal model trained on historical data", flush=True)
            except Exception as e:
                print(f"XGBoost train error: {e}", flush=True)

        pair_suggestions = compute_pairs_suggestions(all_signals, ticker_map)
        if pair_suggestions:
            pair_str = " ".join(f"{p['pair']}={p['ratio']}" for p in pair_suggestions)
            print(f"Pairs: {pair_str}", flush=True)

        actual_idr_balance = config.PLAY_CAPITAL_IDR

        if config.INDODAX_API_KEY and config.INDODAX_SECRET_KEY:
            try:
                info = await get_balance(client)
                bal = info.get("balance", {})
                actual_idr_balance = float(bal.get("idr", 0)) or config.PLAY_CAPITAL_IDR
                for coin, raw_qty in bal.items():
                    qty = float(raw_qty)
                    if qty <= 0 or coin == "idr":
                        continue
                    pair = f"{coin}_idr"
                    if pair in config.STABLECOINS or pair in config.SKIP_COINS:
                        continue
                    last_price = ticker_map.get(pair, {}).get("last", 0)
                    coin_value = qty * last_price
                    pair_min = _pair_meta.get(pair, {}).get("min_base", config.MIN_ORDER_IDR)
                    if coin_value < pair_min:
                        print(f"  {pair}: dust Rp{coin_value:,.0f} < min Rp{pair_min:,} — skip tracking", flush=True)
                        continue

                    old = next((p for p in positions if p["pair"] == pair), None)
                    if old:
                        old["qty"] = qty
                        old["amount_idr"] = qty * (old.get("entry_price") or 1)
                        continue

                    db_pos_pairs = {p.get("pair") for p in persist.load_positions()}
                    if pair in db_pos_pairs:
                        continue

                    entry_price = _ext_entry_prices.get(pair, 0)
                    if entry_price == 0 and last_price:
                        entry_price = last_price
                        _ext_entry_prices[pair] = entry_price
                        persist.save_entry_prices(_ext_entry_prices)
                    if entry_price > 0:
                        print(f"  {pair}: entry_price={entry_price:,}", flush=True)

                    positions.append({
                        "pair": pair, "side": "BUY",
                        "entry_price": entry_price,
                        "qty": qty,
                        "amount_idr": qty * (entry_price or 1),
                        "atr_pct": None,
                        "entry_time": time.time(),
                    })
                    print(f"  {pair}: restored to positions", flush=True)
                bal_coins = {f"{c}_idr" for c in bal if c != "idr" and float(bal[c]) > 0}
                for p in list(positions):
                    if p["pair"] not in bal_coins:
                        print(f"  CLEANUP: {p['pair']} gak ada di balance — hapus", flush=True)
                        positions.remove(p)
                        persist.save_positions(positions)
            except Exception as e:
                print(f"Balance fetch error: {e} (using previous balance: Rp{actual_idr_balance:,.0f})", flush=True)

        def _coin_price(pair: str) -> float:
            lt = LIVE_TICKERS.get(pair, {})
            if lt.get("last"):
                return lt["last"]
            tm = ticker_map.get(pair, {})
            if tm.get("last"):
                return tm["last"]
            return 0

        pending_play_capital_pct = config.DEFAULT_PLAY_CAPITAL_PCT
        balance_idr = int(actual_idr_balance * pending_play_capital_pct)
        paper_equity = sum(
            p["qty"] * _coin_price(p["pair"])
            for p in positions
        )
        total_equity = actual_idr_balance + paper_equity
        max_positions = config.max_positions_for_equity(total_equity)
        saved_peak = persist.load_peak_capital()
        if saved_peak and saved_peak > portfolio_risk.peak_capital:
            portfolio_risk.peak_capital = saved_peak
        if total_equity > portfolio_risk.peak_capital:
            persist.save_peak_capital(total_equity)

        daily_limit = risk.check_daily_limits(total_equity)
        if daily_limit == "DAILY_LOSS_LIMIT":
            msg = f"🛑 DAILY LOSS LIMIT HIT! Equity: Rp{total_equity:,.0f}. Stop trading hari ini."
            await send_message(msg)
            print(msg, flush=True)
            return

        if portfolio_risk.check_portfolio_stop(total_equity):
            msg = (f"⚠️ PORTFOLIO DRAWDOWN {config.PORTFOLIO_STOP_LOSS_PCT*100}% — "
                   f"Reducing play capital. Equity: Rp{total_equity:,.0f}")
            await send_message(msg)
            print(msg, flush=True)

        if risk.should_stop_trading(total_equity):
            await send_message(f"⚠️ Daily loss warning — Equity: Rp{total_equity:,.0f}.")

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
            except Exception as e:
                print(f"HMM train error: {e}", flush=True)

        orderbooks = {}
        top_pairs = list(ohlcv_map_1h.keys())[:5]
        for pid in top_pairs:
            try:
                ob = await fetch_orderbook(client, pair=pid, depth=10)
                if ob:
                    orderbooks[pid] = ob
            except Exception:
                pass

        can_trade = actual_idr_balance >= config.MIN_ORDER_IDR
        has_active_signal = any(
            s.get("raw_signal") in ("BUY", "SELL") and s.get("score", 0) >= 3 for s in all_signals.values()
        )
        has_positions = len(positions) > 0
        nothing_interesting = not has_active_signal and not has_positions
        equity_changed = abs(total_equity - _prev_equity) / max(_prev_equity, 1) > 0.05 if _prev_equity > 0 else False
        regime_changed = regime_info["regime"] != _prev_regime if _prev_regime else False
        signal_count = sum(1 for s in all_signals.values() if s.get("raw_signal") in ("BUY", "SELL"))
        new_signals = signal_count > _prev_signal_count + 1 if _prev_signal_count > 0 else False

        needs_deepseek = (can_trade or has_positions) and (has_active_signal or regime_changed or equity_changed or new_signals or (cycle_counter % 6 == 0))
        skip_llm = not needs_deepseek

        _prev_equity = total_equity
        _prev_regime = regime_info["regime"]
        _prev_signal_count = signal_count

        if skip_llm:
            if cycle_counter % 12 == 0:
                print(f"LLM SKIPPED — no cash to trade (cycle {cycle_counter})", flush=True)
            decision = {"decision": "HOLD", "reasoning": "No cash to trade — ATR SL/TP aktif", "trades": []}
        else:
            print("Calling DeepSeek portfolio manager...", flush=True)
            portfolio_pnl = ((total_equity - config.PLAY_CAPITAL_IDR) / config.PLAY_CAPITAL_IDR * 100
                             if config.PLAY_CAPITAL_IDR else 0)
            micro_features = {}
            for pid in list(ohlcv_map_1h.keys())[:5]:
                ob = orderbooks.get(pid)
                mf = MicrostructureFeatures.compute_all(ohlcv_map_1h.get(pid, []), ob)
                if mf:
                    micro_features[pid] = mf

            decision = evaluate_portfolio(all_signals, ticker_map, current_positions_info,
                                           actual_idr_balance, portfolio_pnl,
                                           regime_info, pair_suggestions, regime_history, orderbooks,
                                           LIVE_TICKERS, new_coins, pair_signals,
                                           micro_features=micro_features)
            if decision.get("deepseek_error"):
                await send_message(f"⚠️ DeepSeek API error: {decision.get('reasoning', '')[:200]}")
            print(f"PM decision: {decision.get('decision')} | {decision.get('reasoning', '')[:100]}", flush=True)

        play_capital_pct = decision.get("play_capital_pct", pending_play_capital_pct * 100)
        if actual_idr_balance < config.MIN_ORDER_IDR:
            print(f"Cash Rp{actual_idr_balance:,.0f} < min Rp{config.MIN_ORDER_IDR:,}. Skipping buys.", flush=True)
            balance_idr = 0
        else:
            min_balance_needed = config.MIN_ORDER_IDR * 1.2
            if actual_idr_balance < min_balance_needed * 3:
                play_capital_pct = max(play_capital_pct, 80)
            balance_idr = int(actual_idr_balance * play_capital_pct / 100)
            balance_idr = max(balance_idr, config.MIN_ORDER_IDR)
        print(f"CIO play capital: {play_capital_pct}% of Rp{actual_idr_balance:,.0f} = Rp{balance_idr:,}", flush=True)

        log_decision("PORTFOLIO", decision.get("decision", "HOLD"),
                     decision.get("reasoning", ""),
                     executed=len(decision.get("trades", [])) > 0)

        sl_hits = []
        for p in list(positions):
            last = ticker_map.get(p["pair"], {}).get("last", p["entry_price"])
            atr_val = p.get("atr_pct") or (risk.compute_atr(ohlcv_map_1h.get(p["pair"], [])) if p.get("pair") in ohlcv_map_1h else None)
            result = risk.check_sl_tp(p["entry_price"], last, p["side"], pair=p["pair"], atr_pct=atr_val)
            if not result and atr_val:
                atr_sl = atr_val * config.ATR_SL_MULTIPLIER
                dyn_sl = p["entry_price"] * (1 - atr_sl / 100) if p["side"] == "BUY" else p["entry_price"] * (1 + atr_sl / 100)
                if (p["side"] == "BUY" and last <= dyn_sl) or (p["side"] == "SELL" and last >= dyn_sl):
                    result = "ATR_SL"
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
                _cooldown[p["pair"]] = time.time()
                print(f"COOLDOWN: {p['pair']} set for 12h", flush=True)
                if not config.PAPER_TRADING and config.INDODAX_API_KEY:
                    try:
                        coin_name = p["pair"].split("_")[0]
                        _ts_s = int(time.time() * 1000)
                        sp = {"method":"trade","timestamp":_ts_s,"recvWindow":"5000","pair":p["pair"],"type":"sell",
                              coin_name: fmt_qty(p["pair"], p["qty"]), "order_type":"market"}
                        sb = urlencode(sp)
                        ss = hmac.new(config.INDODAX_SECRET_KEY.encode(),sb.encode(),hashlib.sha512).hexdigest()
                        sr = await client.post(config.INDODAX_TAPI_URL, headers={
                            "Key":config.INDODAX_API_KEY,"Sign":ss,
                            "Content-Type":"application/x-www-form-urlencoded",
                        }, content=sb)
                        sj = sr.json()
                        if sj.get("success") == 1:
                            print(f"  SOLD {p['pair']} at market", flush=True)
                            positions.remove(p)
                            persist.save_positions(positions)
                            sell_value = last * p["qty"]
                            log_trade("sell", last, p["qty"], sell_value,
                                      status="closed", pnl=pnl, reason=result)
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
            if "SL_HIT" in sl_hit or "TRAILING_SL" in sl_hit:
                pair = sl_hit.split(" ")[0].replace(":", "")
                _coin_blacklist.add(pair)
                print(f"BLACKLIST: {pair} added (hit stop loss)", flush=True)
        if len(_coin_blacklist) > 20:
            _coin_blacklist.clear()

        if sl_hits:
            await send_message("SL/TP triggered:\n" + "\n".join(sl_hits))
            if config.AUTO_COMPOUND:
                for sl_hit in sl_hits:
                    if "+" in sl_hit.split(":")[-1]:
                        try:
                            pnl_str = sl_hit.split(":")[-1].strip().split(" ")[0]
                            pnl_val = float(pnl_str.replace("+", "").replace(",", ""))
                            if pnl_val > 0:
                                new_capital = min(config.PLAY_CAPITAL_IDR + pnl_val * 0.5, 500_000)
                                config.PLAY_CAPITAL_IDR = new_capital
                                print(f"COMPOUND: Capital grown to Rp{new_capital:,.0f}", flush=True)
                        except Exception:
                            pass

        trades_today = get_trade_count_today()
        if trades_today >= config.MAX_DAILY_TRADES:
            print(f"MAX TRADES/DAY ({config.MAX_DAILY_TRADES}) reached. Skipping new buys.", flush=True)
            decision["trades"] = [t for t in decision.get("trades", []) if t.get("action") != "BUY"]

        trades = decision.get("trades", [])
        all_held = {p["pair"] for p in positions}
        bot_pair_set = {p["pair"] for p in positions}
        trades = [t for t in trades if t.get("action") != "SELL" or t["pair"] in all_held]
        profit_sells = []
        for t in list(trades):
            if t.get("action") == "SELL":
                sell_pair = t["pair"]
                match = next((p for p in positions if p["pair"] == sell_pair), None)
                if match:
                    price_now = ticker_map.get(sell_pair, {}).get("last", 0)
                    entry = match.get("entry_price", 0)
                    pnl = (price_now - entry) / entry * 100 if entry else 0
                    if pnl >= 2:
                        profit_sells.append(t)
                        print(f"PROFIT ROTATE: sell {sell_pair} (+{pnl:.1f}%)", flush=True)
        trades = [t for t in trades if not (t.get("action") == "SELL" and t not in profit_sells)]
        if cycle_counter <= 1:
            if any(t.get("action") == "SELL" for t in decision.get("trades", [])):
                print("STARTUP GUARD: blocked CIO sells (positions restored from balance)", flush=True)
            trades = [t for t in trades if t.get("action") != "SELL"]
        trades = [t for t in trades if t.get("action") != "BUY" or t["pair"] not in _coin_blacklist]
        if config.SKIP_COINS:
            trades = [t for t in trades if t.get("action") != "BUY" or t["pair"] not in config.SKIP_COINS]
        if _coin_blacklist:
            blocked = [t for t in decision.get("trades", []) if t.get("action") == "BUY" and t["pair"] in _coin_blacklist]
            if blocked:
                print(f"BLACKLIST: Skipped BUY for {', '.join(t['pair'] for t in blocked)}", flush=True)
        selling_pairs = {t["pair"] for t in trades if t.get("action") == "SELL"}
        extra_buys = [t for t in trades if t.get("action") == "BUY" and t["pair"] in all_held]
        new_buys = [t for t in trades if t.get("action") == "BUY" and t["pair"] not in all_held]
        slots_left = max(0, max_positions - len(all_held - selling_pairs))
        if len(new_buys) > slots_left:
            trades = [t for t in trades if t.get("action") == "SELL"] + extra_buys + new_buys[:slots_left]
            print(f"Limited new buys to {slots_left} (max {max_positions} unique, equity Rp{total_equity:,.0f})", flush=True)

        if not trades:
            print(f"Cycle done in {int(time.time() - _t0)}s. Sleeping.", flush=True)
            if cycle_counter % 6 == 0 or cycle_counter == 1:
                await send_message(
                    f"🤖 FMA ALPHA — Cycle #{cycle_counter}\n"
                    f"Regime: {regime_info['regime']} | {len(positions)} pos | Cash: Rp{actual_idr_balance:,.0f}"
                )
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
                qty = match["qty"]
                ticker = ticker_map.get(pid, {})
                price = ticker.get("buy", 0)
                if not price:
                    continue
                amount = qty * price
                t["entry_price"] = match.get("entry_price", 0)
                t["exec_price"] = price
            else:
                amount = balance_idr * (alloc / 100)
                ticker = ticker_map.get(pid, {})
                price = ticker.get("sell" if action == "BUY" else "buy", 0)
                if not price:
                    continue
                qty = amount / price

            ohlcv = ohlcv_map_1h.get(pid)
            atr_pct = risk.compute_atr(ohlcv) if ohlcv else None
            if not risk.is_profit_viable(price, qty, action, atr_pct=atr_pct):
                print(f"  {pid}: skipped - fees eat profit", flush=True)
                continue

            print(f"  {action} {pid} @ {price} | Rp{amount:,.0f} ({qty:.6f}) | alloc: {alloc}%", flush=True)

            tp_limit_price = 0
            if atr_pct and action == "BUY":
                sl, tp = risk.get_sl_tp(price, action, atr_pct)
                tp_limit_price = int(tp)
                print(f"  ATR: {atr_pct}% | SL: {sl} | TP: {tp}", flush=True)

            ot = "maker_first" if (config.MAKER_FIRST and action == "BUY") else "market"
            try:
                order = await place_order(client, action.lower(), price, amount,
                                           pair=pid, order_type=ot)
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
                      order_type="limit" if config.PAPER_TRADING else ("maker" if ot == "maker_first" else "market"),
                      status="simulated" if config.PAPER_TRADING else "placed",
                       reason=t.get("reason", ""))
            executed_trades.append(t)

            if action == "BUY":
                positions.append({
                    "pair": pid,
                    "side": action,
                    "entry_price": price,
                    "qty": qty,
                    "amount_idr": amount,
                    "atr_pct": atr_pct if ohlcv else None,
                    "entry_time": time.time(),
                })
                persist.save_positions(positions)
            elif action == "SELL":
                positions = [p for p in positions if p["pair"] != pid]
                persist.save_positions(positions)
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

        global _latest_regime, _latest_ticker_map, _latest_all_signals, _latest_ohlcv_map_1h, _last_actual_balance
        _latest_regime = regime_info
        _latest_ticker_map = ticker_map
        _latest_all_signals = all_signals
        _latest_ohlcv_map_1h = ohlcv_map_1h
        _last_actual_balance = actual_idr_balance

    except Exception as e:
        print(f"Portfolio cycle error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await send_message(f"Portfolio cycle error: {e}")
        except Exception:
            pass
    finally:
        print(f"⏱ Cycle #{cycle_counter} finished in {int(time.time() - _t0)}s", flush=True)

_latest_balance: float = 0

async def _balance_poller(client: httpx.AsyncClient):
    global _latest_balance
    while not shutdown_flag:
        try:
            info = await get_balance(client)
            _latest_balance = float(info.get("balance", {}).get("idr", 0))
        except Exception as e:
            print(f"Balance poller error: {e}", flush=True)
        await asyncio.sleep(30)

async def main():
    global _latest_balance, shutdown_flag

    print("=" * 50, flush=True)
    print("  FMA ALPHA QUANT LABS — INDODAX", flush=True)
    print(f"  Target: Rp{config.PLAY_CAPITAL_IDR:,} → Rp500.000 🔥", flush=True)
    print(f"  Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}", flush=True)
    print(f"  CIO manages play capital dynamically", flush=True)
    print(f"  Model: {config.DEEPSEEK_MODEL}", flush=True)
    print(f"  Max positions: {config.MAX_OPEN_POSITIONS} (dynamic: {config.max_positions_for_equity(config.PLAY_CAPITAL_IDR)}-6)", flush=True)
    print(f"  CIO selects coins from top {config.MAX_SCAN_PAIRS} by volume", flush=True)
    print(f"  Mode: {'🔴 ALPHA' if config.ALPHA_MODE else ' STANDARD'} | SL ATR×{config.ATR_SL_MULTIPLIER:.0f} | TP ATR×{config.ATR_TP_MULTIPLIER:.0f}", flush=True)
    print("=" * 50, flush=True)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        init_db()
        init_chat_db()
        saved = persist.load_positions()
        if saved:
            positions.extend(saved)
        print("DB init OK", flush=True)
        _ext_entry_prices.update(persist.load_entry_prices())
        print(f"Loaded {len(_ext_entry_prices)} entry prices from DB", flush=True)
        recent = get_recent_trades(limit=100)
        portfolio_risk.set_trade_history(recent)
        print(f"Kelly: {len(recent)} trades loaded, optimal f={portfolio_risk.kelly.optimal_fraction():.2f}", flush=True)
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

    ok = await send_message(
        f"🤖 FMA ALPHA QUANT LABS started\n"
        f"CIO aktif — target Rp200k → Rp500k 🔥\n"
        f"CIO scans top {config.MAX_SCAN_PAIRS} pairs by volume\n"
        f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'} | Alpha Mode ON\n"
        f"SL ATR×{config.ATR_SL_MULTIPLIER:.0f} | TP ATR×{config.ATR_TP_MULTIPLIER:.0f}\n"
        f"Notifikasi hanya event-based (no spam tiap 5 menit)"
    )
    print(f"Telegram: {'OK' if ok else 'FAILED'}", flush=True)

    async def _cio_reply(user_msg: str) -> str | None:
        global _latest_regime
        if not config.DEEPSEEK_API_KEY:
            return None

        cash_info = f"Rp{_latest_balance:,.0f}" if _latest_balance else "?"
        pos_lines = []
        for p in positions[:10]:
            pid = p["pair"]
            lp = LIVE_TICKERS.get(pid, {}).get("last") or _latest_ticker_map.get(pid, {}).get("last") or p.get("entry_price", 0)
            pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
            pos_lines.append(f"  {pid}: {p['qty']:.4f} @ {p.get('entry_price',0):,.0f} ({pnl:+.2f}%)")
        pos_str = "\n".join(pos_lines) or "  Tidak ada"

        regime_name = _latest_regime.get("regime", "?")
        chat_history = get_chat_history(8)
        messages = [
            {"role": "system", "content": (
                f"Kamu CIO FMA ALPHA QUANT LABS. Target: Rp{config.PLAY_CAPITAL_IDR:,} → Rp500.000.\n"
                f"Bicara sopan pakai 'aku', singkat (1-3 kalimat), santai.\n"
                f"Cash: {cash_info}\n"
                f"Positions:\n{pos_str}\n"
                f"Regime saat ini: {regime_name}\n"
                f"Mode: {'🔴 ALPHA' if config.ALPHA_MODE else 'STANDARD'} ATR-based SL/TP"
            )},
        ]
        for h in chat_history:
            messages.append({"role": "user" if h["role"] == "user" else "assistant", "content": h["message"]})
        messages.append({"role": "user", "content": user_msg})

        try:
            from openai import OpenAI
            cl = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
            resp = cl.chat.completions.create(model=config.DEEPSEEK_MODEL, messages=messages)
            return resp.choices[0].message.content or "Gak tau."
        except Exception as e:
            return f"Error: {str(e)[:60]}"

    async def _build_coin_detail(pair: str) -> str:
        pid = pair if pair.endswith("_idr") else f"{pair}_idr"
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

                            save_chat("user", raw)

                            if txt in ("/start", "/status"):
                                pos_lines = []
                                for p in positions[:10]:
                                    lp = LIVE_TICKERS.get(p["pair"], {}).get("last") or _latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                    pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
                                    pos_lines.append(f"{p['pair']} {p['side']} @ {p['entry_price']:,.0f} ({pnl:+.2f}%)")
                                text = (
                                    f"FMA ALPHA QUANT LABS 🤖\n"
                                    f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'} | {_latest_regime.get('regime','?')}\n"
                                    f"Cash: Rp{_last_actual_balance:,.0f} | Posisi: {len(positions)}\n" +
                                    ("\n".join(pos_lines) if pos_lines else "Tidak ada posisi")
                                )
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": text},
                                    )
                                save_chat("assistant", text)
                                continue

                            if txt.startswith("/ask "):
                                coin = txt.split("/ask ", 1)[1].strip().upper()
                                detail = await _build_coin_detail(coin)
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": detail},
                                    )
                                save_chat("assistant", detail)
                                continue

                            if txt == "/why":
                                reason = "Tidak ada trade karena:"
                                if _last_actual_balance < config.MIN_ORDER_IDR:
                                    reason += f"\n- Cash Rp{_last_actual_balance:,.0f} < minimum Rp{config.MIN_ORDER_IDR:,}"
                                if not positions:
                                    reason += "\n- Tidak ada posisi"
                                else:
                                    for p in positions[:5]:
                                        lp = LIVE_TICKERS.get(p["pair"], {}).get("last") or _latest_ticker_map.get(p["pair"], {}).get("last") or p.get("entry_price", 0)
                                        pnl = pnl_pct(p.get("entry_price") or 0, lp, p["side"])
                                        if pnl < 2:
                                            reason += f"\n- {p['pair']} ({pnl:+.2f}%) belum ≥2% profit"
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": reason},
                                    )
                                save_chat("assistant", reason)
                                continue

                            if txt.startswith("/"):
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": "Perintah: /status, /ask <coin>, /why"},
                                    )
                                continue

                            if config.DEEPSEEK_API_KEY:
                                async with httpx.AsyncClient() as cc:
                                    await cc.post(
                                        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                        json={"chat_id": cid, "text": "CIO mikir dulu..."},
                                    )
                                reply = await _cio_reply(raw)
                                if reply:
                                    async with httpx.AsyncClient() as cc2:
                                        await cc2.post(
                                            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                            json={"chat_id": cid, "text": reply[:400]},
                                        )
                                    save_chat("assistant", reply)
            except Exception:
                pass
            await asyncio.sleep(5)

    ws_task = asyncio.create_task(market_ws_loop())
    pws_task = asyncio.create_task(private_ws_loop())
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
