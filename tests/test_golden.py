import asyncio, math, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import config
import rules, db
from backtest import SimState, Backtest, FEE_BUY_MARKET, FEE_SELL_MAKER, TAX, CFX

# ================= A. SimState (SM mirror) golden =================
st = SimState("xyz_idr", 100000, 1.0, 2.0, "TP_ACTIVE", ts=0)
assert st.tp_price == 104000 and st.sl_price == 97600  # ATR x2 TP, x1.2 SL
st.tick(high=103000, low=99000, close=102000, ts=1)
assert st.state == "TP_ACTIVE"
st.tick(high=104500, low=103500, close=104000, ts=2)
assert st.state == "CLOSED" and st.exit_reason == "TP" and st.exit_price == 104000
fee_buy = 100000 * 1.0 * FEE_BUY_MARKET
fee_sell = 1.0 * 104000 * (FEE_SELL_MAKER + TAX + CFX)
assert math.isclose(st.pnl(), 4000 - fee_buy - fee_sell, rel_tol=1e-9), st.pnl()
print("A1 OK: TP fill + exact fee math")

st2 = SimState("xyz_idr", 100000, 1.0, 2.0, "TP_ACTIVE", ts=0)
st2.tick(high=100000, low=97000, close=97500, ts=1)   # breach initial SL -> arm SL_ACTIVE
assert st2.state == "SL_ACTIVE"
st2.tick(high=98500, low=96000, close=97000, ts=2)    # low <= sl_price -> close at SL
assert st2.state == "CLOSED" and st2.exit_reason == "SL" and st2.exit_price == 97600
print("A2 OK: SL arming + close at exact SL price")

st3 = SimState("xyz_idr", 100000, 1.0, 2.0, "TRAILING", ts=0)
st3.tick(high=105000, low=104000, close=104500, ts=1)
st3.tick(high=106000, low=101000, close=102000, ts=2)
assert st3.state == "CLOSED" and st3.exit_reason == "TRAILING_SL"
# ratchet follows close-based high (104500), trail distance = max(ATR*1.5, 0.8) = 3%
assert math.isclose(st3.exit_price, 104500 * (1 - 3.0 / 100)), st3.exit_price
print("A3 OK: trailing stop ratchets with new high")

# ================= B. rules.decide golden =================
def mk_sig(**kw):
    base = {"raw_signal": "BUY", "score": 10, "rsi": 50, "volume_ratio": 1.5,
            "atr_pct": 3.0, "ema9": 1005, "ema21": 995, "ema50": 950,
            "momentum_streak": 1, "timeframe_aligned": True}
    base.update(kw)
    return {"xyz_idr": base}

tick = {"xyz_idr": {"sell": 1000, "vol_idr": 6e8}}
ohlc = {"xyz_idr": [{"high": 1010, "low": 990, "close": 1000} for _ in range(20)]}
regime_bull = {"regime": "BULL", "hmm_confidence": 1.0}

dec = rules.decide(mk_sig(), tick, {}, [], 200_000, 200_000, regime_bull, ohlc, set(), {})
buys = [t for t in dec["trades"] if t["action"] == "BUY"]
assert dec["decision"] == "REBALANCE" and len(buys) == 1 and buys[0]["pair"] == "xyz_idr", dec
print("B1 OK: BULL momentum BUY on qualified candidate")

dec = rules.decide(mk_sig(), tick, {}, [], 200_000, 200_000,
                   {"regime": "BEAR", "hmm_confidence": 1.0}, ohlc, set(), {})
assert not [t for t in dec["trades"] if t["action"] == "BUY"], "BEAR must not open longs"
print("B2 OK: BEAR blocks new entries")

pos = [{"pair": "xyz_idr", "side": "BUY", "qty": 1.0, "entry_price": 1000, "entry_time": time.time()}]
dec = rules.decide(mk_sig(raw_signal="HOLD"), {"xyz_idr": {"sell": 900, "vol_idr": 6e8}}, {},
                   pos, 50_000, 140_000, regime_bull, ohlc, set(), {})
sells = [t for t in dec["trades"] if t["action"] == "SELL"]
assert len(sells) == 1 and "Cut" in sells[0]["reason"], dec
print("B3 OK: ATR cut-loss on -10% position")

dec = rules.decide(mk_sig(), tick, {}, [], 200_000, 200_000,
                   {"regime": "HIGH_VOL", "hmm_confidence": 0.5}, ohlc, set(), {})
assert dec["decision"] == "HOLD" and dec["trades"] == []
print("B4 OK: HIGH_VOL survival mode — no entry")

sig_mr = {"xyz_idr": {"raw_signal": "HOLD", "rsi": 30, "bb_lower": 1010,
                      "bb_upper": 1100, "atr_pct": 3.0, "volume_ratio": 1.5}}
dec = rules.decide(sig_mr, tick, {}, [], 100_000, 100_000,
                   {"regime": "SIDEWAYS", "hmm_confidence": 0.5}, None, set(), {})
assert [t for t in dec["trades"] if t["action"] == "BUY"], dec
print("B5 OK: SIDEWAYS mean-reversion entry on oversold")

pos_mr = [{"pair": "xyz_idr", "side": "BUY", "qty": 1.0, "entry_price": 1000, "entry_time": time.time()}]
sig_mr2 = {"xyz_idr": {"raw_signal": "HOLD", "rsi": 55, "bb_upper": 1005, "atr_pct": 2.0}}
dec = rules.decide(sig_mr2, {"xyz_idr": {"sell": 1010, "vol_idr": 6e8}}, {},
                   pos_mr, 50_000, 151_000, {"regime": "SIDEWAYS", "hmm_confidence": 0.5},
                   None, set(), {})
sells = [t for t in dec["trades"] if t["action"] == "SELL"]
assert len(sells) == 1 and "MR TP" in sells[0]["reason"], dec
print("B6 OK: MR take-profit on +1% (>= 0.5 ATR)")

# DCA_EXCLUSIVE must never enter the momentum path
dec = rules.decide({"btc_idr": mk_sig()["xyz_idr"]}, {"btc_idr": {"sell": 1000, "vol_idr": 6e8}},
                   {}, [], 200_000, 200_000, regime_bull,
                   {"btc_idr": ohlc["xyz_idr"]}, set(), {})
assert not [t for t in dec["trades"] if t["pair"] == "btc_idr"], "DCA-exclusive leaked into rules engine"
print("B7 OK: DCA-exclusive pairs excluded from rules engine")

# ================= C. db paper_trade flag =================
db.DB_PATH = "/tmp/golden_trades.db"
import os
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()
config.PAPER_TRADING = True
db.log_trade("buy", 1000, 1, 1000, reason="p")
config.PAPER_TRADING = False
db.log_trade("buy", 1000, 1, 1000, reason="l")
import sqlite3
with sqlite3.connect(db.DB_PATH) as c:
    rows = c.execute("SELECT paper_trade FROM trades ORDER BY id").fetchall()
assert rows == [(1,), (0,)], rows
print("C1 OK: paper_trade column follows PAPER_TRADING (was hardcoded 1)")

# ================= D. Backtest window-NameError regression =================
bt = Backtest(capital=200000, regime_override="BULL")
win = {"xyz_idr": [{"high": 1010, "low": 990, "close": 1000, "atr_pct": 2.0} for _ in range(30)]}
bt._exec_buy({"pair": "xyz_idr"}, 1000, 10, win)   # used to raise NameError: window
assert "xyz_idr" in bt.states and bt.states["xyz_idr"].atr == 2.0
assert len(bt.positions) == 1 and bt.capital < 200000
print("D1 OK: _exec_buy consumes window (Atr lookup), state machine created")

# end-to-end synthetic run, no network
n = 80
synth = {}
for i in range(n):
    price = 1000 * (1 + 0.004 * math.sin(i / 5.0))
    synth.setdefault("aaa_idr", []).append({"high": price * 1.01, "low": price * 0.99,
                                            "close": price, "open": price, "Time": i})
    synth.setdefault("bbb_idr", []).append({"high": price * 1.008, "low": price * 0.992,
                                            "close": price * 0.999, "open": price, "Time": i})
eng = Backtest(capital=200000, regime_override="BULL")
final = eng.run(synth)
assert isinstance(final, float) or isinstance(final, (int,)), final
assert len(eng.equity_curve) > 1
print(f"D2 OK: full backtest run completes — {len(eng.trades)} trades, final Rp{final:,.0f}")

print("ALL GOLDEN CHECKS PASSED")
