# Copyright (C) 2026 FMA ALPHA QUANT LABS
# See LICENSE for details.

"""Stage 1 #8 — state runtime pindah dari main.py ke engine/state.py.

Invariant yang dijaga: tidak boleh ada nama state yang masih dibaca/ditulis
sebagai global telanjang di main.py. Kalau ada yang menulis `positions.append(...)`
di dalam fungsi engine baru, nama itu lookup ke module scope main.py yang sudah
kosong → NameError saat runtime. Test ini menangkap itu sebelum jalan.
"""

import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import config  # noqa: E402
config.HAS_TRADING_KEYS = False

import main as m  # noqa: E402
from engine import state as S  # noqa: E402

from types import FunctionType, ModuleType  # noqa: E402

# nama state saja: buang modul/class hasil import, dan registry (bukan state —
# memang singleton bersama yang sengaja diimpor ulang oleh konsumen)
STATE_NAMES = {n for n in dir(S)
               if not n.startswith("__") and n != "registry"
               and not isinstance(getattr(S, n), (ModuleType, type, FunctionType))}


def read(name: str) -> str:
    with open(os.path.join(ROOT, name)) as f:
        return f.read()


# --- T1: 50 nama state tinggal di engine.state, bukan lagi di main
assert len(STATE_NAMES) >= 45, f"engine.state terlalu kecil: {len(STATE_NAMES)}"
missing = [n for n in STATE_NAMES if not hasattr(S, n)]
assert not missing, f"nama hilang dari engine.state: {missing}"
stray = [n for n in STATE_NAMES if n in vars(m)]
assert not stray, f"state masih terdefinisi di main.py: {stray}"
print(f"T1 OK: {len(STATE_NAMES)} nama state hanya hidup di engine.state")

# --- T2: tidak ada `global` lagi di main.py (semua rebind lewat S.<nama>)
src = read("main.py")
assert "global " not in src, "main.py masih punya pernyataan global"
print("T2 OK: nol pernyataan global — mutasi state selalu eksplisit lewat S.")

# --- T3: tidak ada referensi telanjang ke nama state di luar main()
# allowlist: correlation_risk punya parameter lokal bernama `positions` (bukan
# state), dan main() punya bug lama shadow circuit-breaker yang sengaja dipertahankan.
tree = ast.parse(src)
funcs = []
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        funcs.append((node.name, node.lineno, node.end_lineno))


def owner(lineno):
    best = None
    for name, lo, hi in funcs:
        if lo <= lineno <= hi and (best is None or lo >= best[1]):
            best = (name, lo, hi)
    return best[0] if best else "<module>"


allow = {"correlation_risk", "main"}
bare = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.Name) or node.id not in STATE_NAMES:
        continue
    o = owner(node.lineno)
    if o in allow:
        continue
    bare.setdefault(o, []).append((node.lineno, node.id))
assert not bare, f"state dipakai telanjang di {sorted(bare)}"
print("T3 OK: tidak ada state global telanjang tersisa di fungsi mana pun (di luar main)")

# --- T4: main.S dan engine.state adalah modul yang sama (share, bukan salin)
assert m.S is S
before = len(S.positions)
S.positions = list(S.positions) + [{"pair": "zzz_idr", "side": "BUY"}]
assert any(p["pair"] == "zzz_idr" for p in m.S.positions)
S.positions = S.positions[:before]
S.cycle_counter += 7
assert m.S.cycle_counter == S.cycle_counter
S.cycle_counter -= 7
print("T4 OK: main.py dan engine.state berbagi objek state yang identik")

# --- T5: engine.state tidak mengimpor main.py (tidak ada circular import)
code = ("import sys; sys.path.insert(0, %r); import engine.state;"
        "assert 'main' not in sys.modules, sorted(k for k in sys.modules if k=='main')" % ROOT)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
print("T5 OK: engine.state bisa diimpor tanpa menyentuh main.py")

# --- T6: engine id source terdaftar sekali dan dibaca dari state baru
assert len(S.registry._engine_sources) == 1
S._position_states["q_idr"] = {"tp_order_id": 7001, "sl_order_id": None}
assert "7001" in S._engine_order_ids()
S._position_states.pop("q_idr")
assert "7001" not in S._engine_order_ids()
print("T6 OK: _engine_order_ids ikut pindah dan tetap jadi sumber id resting order")

# --- T7: fungsi murni main.py masih jalan membaca state lewat S
sig = {"aaa_idr": {"raw_signal": "BUY", "score": 3, "volatility": 1.0, "conviction": "HIGH"},
       "bbb_idr": {"raw_signal": "BUY", "score": 2, "volatility": 1.0, "conviction": "MED"}}
reg = m.classify_regime(sig, None)
assert reg["regime"] in ("BULL", "BEAR", "SIDEWAYS", "HIGH_VOL", "SIDEWAYS_LOW_VOL"), reg
m._sm_init("smtest_idr", 100.0, 1.0, 2.0)
assert m._sm_get("smtest_idr") is not None
assert "smtest_idr" in S._position_states
m._sm_cleanup("smtest_idr")
assert "smtest_idr" not in S._position_states
print("T7 OK: classify_regime + position-SM membaca state lewat S tanpa error")

print("ALL ENGINE-STATE CHECKS PASSED")
