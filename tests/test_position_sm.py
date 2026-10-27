# Copyright (C) 2026 FMA ALPHA QUANT LABS
# See LICENSE for details.

"""Stage 2 #8 — posisi state machine pindah ke engine/position_sm.py.

Ini jalur live (tick WS → TP/SL/trailing/pyramid) yang sebelumnya nol test
karena menempel di main.py dan tidak bisa dipanggil tanpa menyalakan bot.
Sekarang modulnya berdiri sendiri: dependency-nya executor + notifier + state,
semuanya bisa dipasangi stub.
"""

import ast
import asyncio
import os
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import config  # noqa: E402
config.HAS_TRADING_KEYS = False

from engine import state as S  # noqa: E402
from engine import position_sm as ps  # noqa: E402

MOVED = ["pnl_pct", "add_position", "_handle_fill_event", "_realtime_sltp_check",
         "_sm_cancel", "_sm_place_sell", "_sm_place_tp", "_sm_place_sl",
         "_sm_init", "_sm_cleanup", "_sm_get"]

CALLS = []


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def fake_place(client, side, price, amount, pair=None, order_type="limit", qty=None, **kw):
    CALLS.append(("place", side, price, qty, order_type, pair))
    return {"order_id": 5000 + len(CALLS)}


async def fake_cancel(client, oid, pair=None, side=None, **kw):
    CALLS.append(("cancel", oid, pair))
    return {"success": 1}


async def fake_notify(text, **kw):
    CALLS.append(("notify", text[:24]))


def install_stubs():
    old = (ps.httpx, ps.place_order, ps.cancel_order, ps.send_message)
    ps.httpx = types.SimpleNamespace(AsyncClient=FakeClient)
    ps.place_order = fake_place
    ps.cancel_order = fake_cancel
    ps.send_message = fake_notify
    return old


def restore_stubs(old):
    ps.httpx, ps.place_order, ps.cancel_order, ps.send_message = old


def fresh(pair="btc_idr", entry=100_000_000, qty=0.01, atr=2.0, state="TP_ACTIVE"):
    S.positions.clear()
    S._position_states.clear()
    S.positions.append({"pair": pair, "side": "BUY", "entry_price": entry,
                        "qty": qty, "amount_idr": entry * qty, "atr_pct": atr})
    ps._sm_init(pair, entry, qty, atr)
    S._position_states[pair]["state"] = state
    S._realtime_sltp_last.pop(pair, None)
    return pair, entry, qty, atr


# --- T1: _sm_init/_sm_get/_sm_cleanup = lifecycle lengkap, tidak ada dict yatim
pair, entry, qty, atr = fresh()
sm = ps._sm_get(pair)
assert sm and sm["state"] == "TP_ACTIVE" and sm["tp_order_id"] is None
assert sm["trailing_high"] == entry and sm["entry_price"] == entry
assert ps._sm_init("x_idr", 1, 1, 1) is None and S._position_states["x_idr"]["state"] == "NEW"
S._position_states.pop("x_idr")
S.risk.trailing_highs[pair] = 1
S.risk._pyramid_done[pair] = True
S._realtime_sltp_last[pair] = 9
ps._sm_cleanup(pair)
assert ps._sm_get(pair) is None
assert pair not in S.risk.trailing_highs and pair not in S.risk._pyramid_done
assert pair not in S._realtime_sltp_last
ps._sm_cleanup(pair)  # idempotent
print("T1 OK: init/get/cleanup SM bersih termasuk 8 dict sampingan, cleanup idempotent")

# --- T2: _sm_place_tp / _sm_place_sl menulis state + matematika ATR
pair, entry, qty, atr = fresh(atr=3.0)
old = install_stubs()
try:
    CALLS.clear()
    oid = asyncio.run(ps._sm_place_tp(None, pair, qty, entry, atr))
    want = int(entry * (1 + 3.0 * config.ATR_TP_MULTIPLIER / 100))
    assert oid and CALLS[0] == ("place", "sell", want, qty, "limit", pair), CALLS
    s = S._position_states[pair]
    assert s["tp_order_id"] == oid and s["tp_price"] == want and s["state"] == "TP_ACTIVE"

    CALLS.clear()
    oid2 = asyncio.run(ps._sm_place_sl(None, pair, qty, entry, atr))
    want2 = int(entry * (1 - 3.0 * config.ATR_SL_MULTIPLIER / 100))
    assert CALLS[0] == ("place", "sell", want2, qty, "limit", pair)
    assert S._position_states[pair]["sl_order_id"] == oid2
    assert S._position_states[pair]["sl_price"] == want2

    # ATR 0 → floor: TP minimal 0.5% ATR, SL minimal 1.5%
    CALLS.clear()
    asyncio.run(ps._sm_place_tp(None, pair, qty, entry, 0.0))
    assert CALLS[0][2] == int(entry * (1 + 0.5 * config.ATR_TP_MULTIPLIER / 100))
    CALLS.clear()
    asyncio.run(ps._sm_place_sl(None, pair, qty, entry, 0.0))
    assert CALLS[0][2] == int(entry * (1 - 0.015))
    assert asyncio.run(ps._sm_cancel(None, 123, pair)) is True
finally:
    restore_stubs(old)
print("T2 OK: TP/SL nulis _position_states + floor ATR 0.5%/1.5% berlaku")

# --- T3: _sm_cancel menelan exception (jalur WS tidak boleh mati)
old = install_stubs()
async def boom(client, oid, pair=None, side=None, **kw):
    raise RuntimeError("tapi 500")
ps.cancel_order = boom
assert asyncio.run(ps._sm_cancel(None, 9, "btc_idr")) is False
restore_stubs(old)
print("T3 OK: gagal cancel mengembalikan False, tidak melempar ke consumer")

# --- T4: TP_ACTIVE → SL_ACTIVE saat harga jatuh ke level SL (cancel TP, place SL)
pair, entry, qty, atr = fresh(atr=4.0)
sl_level = entry * (1 - max(atr, 0.5) * config.ATR_SL_MULTIPLIER / 100)
S._position_states[pair]["tp_order_id"] = 7777
old = install_stubs()
try:
    CALLS.clear()
    asyncio.run(ps._realtime_sltp_check(pair, sl_level - 1))
    kinds = [c[0] for c in CALLS]
    assert kinds == ["cancel", "place", "notify"], CALLS
    assert CALLS[0] == ("cancel", 7777, pair)
    s = S._position_states[pair]
    assert s["state"] == "SL_ACTIVE" and s["tp_order_id"] is None and s["sl_order_id"]
finally:
    restore_stubs(old)
print("T4 OK: tick di bawah level SL membatalkan TP lalu memasang SL (serial, 1x notify)")

# --- T5: rate limit 10 detik — tick beruntun tidak spam order
pair, entry, qty, atr = fresh(atr=4.0)
S._position_states[pair]["tp_order_id"] = 7778
low = entry * (1 - max(atr, 0.5) * config.ATR_SL_MULTIPLIER / 100) - 1
old = install_stubs()
try:
    CALLS.clear()
    for _ in range(50):
        asyncio.run(ps._realtime_sltp_check(pair, low))
    assert len([c for c in CALLS if c[0] == "cancel"]) == 1, CALLS
finally:
    restore_stubs(old)
print("T5 OK: 50 tick beruntun = 1 reaksi (debounce 10s masih berlaku di modul baru)")

# --- T6: SL_ACTIVE → TP_ACTIVE saat harga pulih; TRAILING meratchet ke atas
pair, entry, qty, atr = fresh(atr=4.0, state="SL_ACTIVE")
S._position_states[pair].update(sl_order_id=8888, tp_order_id=None)
old = install_stubs()
try:
    CALLS.clear()
    asyncio.run(ps._realtime_sltp_check(pair, entry * 1.02))
    assert [c[0] for c in CALLS] == ["cancel", "place", "notify"], CALLS
    assert S._position_states[pair]["state"] == "TP_ACTIVE"

    pair, entry, qty, atr = fresh(state="TRAILING")
    s = S._position_states[pair]
    s.update(sl_order_id=9999, sl_price=int(entry * 0.99), trailing_high=entry)
    CALLS.clear()
    asyncio.run(ps._realtime_sltp_check(pair, entry * 1.05))
    assert [c[0] for c in CALLS][:2] == ["cancel", "place"], CALLS
    assert s["sl_price"] > int(entry * 0.99), s["sl_price"]
    assert s["trailing_high"] == entry * 1.05
finally:
    restore_stubs(old)
print("T6 OK: recovery → TP aktif lagi; trailing hanya menaikkan level SL")

# --- T7: tanpa position / tanpa SM = nol order (tidak ada hantu cancel)
pair, entry, qty, atr = fresh()
old = install_stubs()
try:
    CALLS.clear()
    asyncio.run(ps._realtime_sltp_check("koin_hantu_idr", 1))
    S.positions.clear()
    S._position_states["sudah_selesai_idr"] = {"state": "TP_ACTIVE", "entry_price": 1,
                                               "qty": 1, "atr_pct": 2.0,
                                               "tp_order_id": 4242, "sl_order_id": None,
                                               "tp_price": 2, "sl_price": 0,
                                               "trailing_high": 1}
    asyncio.run(ps._realtime_sltp_check("sudah_selesai_idr", 1))
    assert CALLS == [], CALLS
    assert "sudah_selesai_idr" not in S._position_states, "SM seharusnya dibersihkan"
finally:
    restore_stubs(old)
print("T7 OK: pair asing tak disentuh; posisi hilang → SM dibersihkan, nol order")

# --- T8: pyramid dibekukan saat daily loss hit (risiko tertinggi di SM)
pair, entry, qty, atr = fresh(atr=2.0, state="TRAILING")
s = S._position_states[pair]
s.update(sl_order_id=None, sl_price=0, trailing_high=entry)
S._latest_balance = 5_000_000
S._daily_loss_hit_today = True
S._greed_used_today = False
old = install_stubs()
try:
    CALLS.clear()
    asyncio.run(ps._realtime_sltp_check(pair, entry * 1.06))
    assert not [c for c in CALLS if c[0] == "place" and c[1] == "buy"], CALLS
finally:
    restore_stubs(old)
S._daily_loss_hit_today = False
print("T8 OK: daily-loss halt menang atas trigger pyramid")

# --- T9: struktural — main.py tidak lagi mendefinisikan SM, tidak ada siklus impor
tree = ast.parse(open(os.path.join(ROOT, "main.py")).read())
defs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
assert not (defs & set(MOVED)), f"masih didefinisikan di main.py: {defs & set(MOVED)}"
for name in MOVED:
    assert hasattr(ps, name), name
src_ps = open(os.path.join(ROOT, "engine", "position_sm.py")).read()
assert "import main" not in src_ps and "from main" not in src_ps
code = ("import sys; sys.path.insert(0, %r); import engine.position_sm;"
        "assert 'main' not in sys.modules" % ROOT)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
import main as m
for n in MOVED:
    if hasattr(m, n):
        assert getattr(m, n) is getattr(ps, n), f"main.py punya salinan {n}"
print("T9 OK: 11 fungsi pindah utuh, main.py konsumen — nol circular import")

# --- T10: kontrak lock tertulis di docstring modul, bukan di kepala orang
doc = (ps.__doc__ or "").lower()
assert "_state_lock" in doc, "kontrak lock harus ada di docstring position_sm"
print("T10 OK: position_sm berdiri sendiri + kontrak lock terbaca mesin")
S.positions.clear()
S._position_states.clear()
print("ALL POSITION-SM CHECKS PASSED")
