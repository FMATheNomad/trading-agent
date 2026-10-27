# Copyright (C) 2026 FMA ALPHA QUANT LABS
# See LICENSE for details.

"""Stage 3 #8 — portfolio_cycle pindah ke engine/cycle.py.

`portfolio_cycle` dulu 1.290 baris di dalam entrypoint, jadi tidak ada satu pun
jalur keputusannya yang teruji. Test ini mengunci bentuk hasil pecahan: definisi
hanya ada di engine/cycle.py, modulnya berdiri sendiri (nol dependency ke
main.py), dan dua keputusan terawal (circuit breaker) benar-benar dieksekusi.
"""

import ast
import asyncio
import contextlib
import io
import builtins
import os
import subprocess
import time
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import config  # noqa: E402
config.HAS_TRADING_KEYS = False

from engine import state as S  # noqa: E402
from engine import cycle as C  # noqa: E402

CYCLE_SRC = open(os.path.join(ROOT, "engine", "cycle.py"), encoding="utf-8").read()
MAIN_SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
MOVED = ["classify_regime", "correlation_risk", "portfolio_cycle"]


def defs(src):
    tree = ast.parse(src)
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# --- T1: struktur — definisi hanya di engine/cycle.py, main.py tinggal orkestrasi
cd, md = defs(CYCLE_SRC), defs(MAIN_SRC)
assert all(n in cd for n in MOVED), f"hilang dari engine/cycle.py: {[n for n in MOVED if n not in cd]}"
assert not (set(md) & set(MOVED)), f"masih didefinisikan di main.py: {set(md) & set(MOVED)}"
assert set(md) == {"_on_ws_tick", "_on_ws_fill", "_state_consumer", "handle_sig",
                   "_optimizer_loop", "_balance_poller", "_momentum_scanner", "main"}, sorted(md)
main_lines = MAIN_SRC.count("\n") + 1
assert main_lines < 900, f"main.py masih {main_lines} baris"
print(f"T1 OK: main.py {main_lines} baris = hanya entrypoint/loop, "
      f"{len(defs(CYCLE_SRC))} fungsi keputusan di engine/cycle.py")

# --- T2: tidak ada nama yang bocor dari main.py (audit seluruh Name load)
bi = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def bound_names(node):
    b = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            b.add(x.id)
        elif isinstance(x, ast.arg):
            b.add(x.arg)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            b.update(a.asname or a.name.split(".")[0] for a in x.names)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b.add(x.name)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            b.add(x.name)
        elif isinstance(x, ast.comprehension):
            b.update(t.id for t in ast.walk(x.target) if isinstance(t, ast.Name))
    return b


tree = ast.parse(CYCLE_SRC)
mod_bound = bound_names(tree)
leaks = {}
for x in ast.walk(tree):
    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in mod_bound | bi:
        leaks.setdefault(x.id, x.lineno)
assert not leaks, f"engine/cycle.py membaca nama yang tidak dia punya: {leaks}"
print(f"T2 OK: nol nama bocor dari main.py — semua {len(mod_bound)} binding milik engine/cycle.py sendiri")

# --- T3: engine/cycle.py bisa diimpor tanpa main.py (nol circular import)
assert "import main" not in CYCLE_SRC and "from main" not in CYCLE_SRC
code = ("import sys; sys.path.insert(0, %r); import engine.cycle;"
        "assert 'main' not in sys.modules" % ROOT)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
print("T3 OK: engine.cycle dimuat tanpa menyentuh main.py")

# --- T4: classify_regime — threshold rules + override HMM
sig_bull = {f"c{i}_idr": {"raw_signal": "BUY", "score": 3, "volatility": 1.0,
                          "conviction": "HIGH"} for i in range(5)}
sig_bull.update({f"s{i}_idr": {"raw_signal": "SELL", "score": -1, "volatility": 1.0,
                               "conviction": "MED"} for i in range(5)})
assert C.classify_regime(sig_bull, None)["regime"] == "BULL"
sig_bear = {f"s{i}_idr": {"raw_signal": "SELL", "score": -3, "volatility": 1.0,
                          "conviction": "MED"} for i in range(6)}
sig_bear.update({"b0_idr": sig_bull["c0_idr"], "b1_idr": sig_bull["c1_idr"]})
assert C.classify_regime(sig_bear, None)["regime"] == "BEAR"
sig_calm = {f"x{i}_idr": {"raw_signal": "HOLD", "score": 0, "volatility": 0.1,
                          "conviction": "LOW"} for i in range(6)}
assert C.classify_regime(sig_calm, None)["regime"] == "SIDEWAYS_LOW_VOL"
sig_hv = {f"x{i}_idr": {"raw_signal": "HOLD", "score": 0, "volatility": 9.0,
                        "conviction": "LOW"} for i in range(6)}
assert C.classify_regime(sig_hv, None)["regime"] == "HIGH_VOL"


class FakeHMM:
    def predict(self, ohlcv):
        return {"regime": "BEAR", "confidence": 0.91}


real_hmm = S.hmm_detector
S.hmm_detector = FakeHMM()
try:
    out = C.classify_regime(sig_bull, {"a_idr": [[1, 2, 3]]})
    assert out["regime"] == "BEAR" and out["hmm_confidence"] == 0.91, out
    assert out["total_signals"] == 10 and out["high_conviction_count"] == 5
finally:
    S.hmm_detector = real_hmm
print("T4 OK: 4 jalur rules + HMM confidence>0.6 menimpa rules, metadata tetap diisi")

# --- T5: correlation_risk — harga bersebelahan = risiko konsentrasi
pos = [{"pair": "btc_idr", "entry_price": 100_000_000}, {"pair": "eth_idr", "entry_price": 50_000_000}]
assert C.correlation_risk(pos[:1], {}, 0.85) == [], "1 posisi belum bisa dikorelasikan"
assert C.correlation_risk(pos, {"btc_idr": {"last": 100_000_000},
                                "eth_idr": {"last": 104_000_000}}, 0.85) == ["btc_idr+eth_idr"]
assert C.correlation_risk(pos, {"btc_idr": {"last": 100_000_000},
                                "eth_idr": {"last": 400_000_000}}, 0.85) == []
print("T5 OK: rasio harga <1.1 ditandai, >1.1 bersih, posisi tunggal diabaikan")

# --- T6: main.py tetap satu-satunya pemanggil cycle dan selalu di bawah lock
assert MAIN_SRC.count("await portfolio_cycle(client)") == 1
assert "async with S._state_lock:\n                await portfolio_cycle(client)" in MAIN_SRC
assert "import main" not in CYCLE_SRC
print("T6 OK: portfolio_cycle dipanggil sekali, selalu dalam S._state_lock")

# --- T7: circuit breaker aktif = cycle pulang cepat tanpa menyentuh exchange
snap = (S._cb_active_until, S._cb_consecutive_loss_days, S.cycle_counter, S.risk.daily_loss_stopped)
try:
    S._cb_active_until = time.time() + 3600
    S.cycle_counter = 0
    asyncio.run(C.portfolio_cycle(None))  # client=None: kalau lanjut, langsung AttributeError
    assert S.cycle_counter == 1, "cycle counter harus tetap naik walau diblokir"
    assert S.risk.daily_loss_stopped is False
    print("T7 OK: breaker aktif -> 0 panggilan API, counter tetap naik")

    # --- T8: breaker kedaluwarsa = status dibersihkan + persisted + dinotifikasi
    saved, sent = [], []
    real_save, real_send, real_fetch = C.persist.save_circuit_breaker, C.send_message, C.fetch_viable_pairs

    async def boom(client):
        raise ConnectionError("sentinel-tiada-jaringan")

    def fake_save(d):
        saved.append(d)

    async def fake_send(t, **k):
        sent.append(t)

    C.persist.save_circuit_breaker = fake_save
    C.send_message = fake_send
    C.fetch_viable_pairs = boom
    buf = io.StringIO()
    try:
        S._cb_active_until = time.time() - 1
        S._cb_consecutive_loss_days = 3
        S._cb_triggered_at = 123
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
            asyncio.run(C.portfolio_cycle(None))
    finally:
        C.persist.save_circuit_breaker = real_save
        C.send_message = real_send
        C.fetch_viable_pairs = real_fetch
    assert S._cb_active_until == 0 and S._cb_consecutive_loss_days == 0 and S._cb_triggered_at == 0
    assert saved == [{"consecutive_loss_days": 0, "last_loss_date": "",
                      "triggered_at": 0, "active_until": 0}], saved
    assert sent and "CIRCUIT BREAKER" in sent[0]
    assert "sentinel-tiada-jaringan" in buf.getvalue(), buf.getvalue()[-400:]
    print("T8 OK: breaker kedaluwarsa direset ke persist + notifikasi, cycle lanjut "
          "dan gagal-safe di batas jaringan")
finally:
    (S._cb_active_until, S._cb_consecutive_loss_days, S.cycle_counter,
     S.risk.daily_loss_stopped) = snap

print("ALL CYCLE-MODULE CHECKS PASSED")
