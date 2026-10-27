import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
config.PAPER_TRADING = True
import main as m
from orders import OrderRegistry, ENTRY, SELL, GRID_BUY, DCA_TP, Tracked
from grid_mini import GridInstance
from dca_smart import DCAInstance

# --- T1: one live order per (pair, kind); re-adding replaces, never duplicates
r = OrderRegistry()
a = r.add("btc_idr", ENTRY, 111, side="buy", price=600_000_000, qty=0.001, amount_idr=600_000)
b = r.add("btc_idr", ENTRY, 222, side="buy", price=600_000_000, qty=0.001, amount_idr=600_000)
assert len(r) == 1 and r.get("btc_idr", ENTRY).order_id == "222", r.list()
assert a is not b and isinstance(a, Tracked)
r.add("btc_idr", SELL, 333, side="sell", qty=0.001)
assert len(r) == 2
assert r.has("btc_idr") and r.has("eth_idr") is False
assert r.has("btc_idr", SELL) and r.has("btc_idr", DCA_TP) is False
print("T1 OK: registry keyed by (pair, kind) — satu order aktif per kombinasi")

# --- T2: unknown kind rejected loudly (typo di call site = bug diam)
try:
    r.add("btc_idr", "pending", 1)
    raise AssertionError("should raise")
except ValueError as e:
    assert "unknown order kind" in str(e), e
print("T2 OK: kind salah nama langsung ValueError")

# --- T3: find/pop/drop_pair
assert r.find(333).pair == "btc_idr" and r.find("222").kind == ENTRY
assert r.find(999) is None
popped = r.pop("btc_idr", ENTRY)
assert popped.order_id == "222" and r.get("btc_idr", ENTRY) is None
assert r.pop("btc_idr", ENTRY) is None
r.add("dogi_idr", GRID_BUY, 444)
r.add("dogi_idr", DCA_TP, 555)
dropped = r.drop_pair("dogi_idr")
assert {t.order_id for t in dropped} == {"444", "555"}, dropped
assert r.tracked_pairs() == {"btc_idr"}, r.tracked_pairs()
print("T3 OK: find/pop/drop_pair + tracked_pairs")

# --- T4: bump = grace counter; 0 untuk order yang sudah tidak dilacak
r.add("eth_idr", ENTRY, 666, is_maker=True)
assert r.bump("eth_idr", ENTRY) == 1
assert r.bump("eth_idr", ENTRY) == 2
assert r.bump("solow_idr", ENTRY) == 0
print("T4 OK: bump naikkan cycles, balik 0 kalau order sudah hilang")

# --- T5: list() snapshot — aman diiterasi sambil pop (loop reconcile)
r2 = OrderRegistry()
for i in range(4):
    r2.add(f"p{i}_idr", ENTRY, 700 + i)
    r2.add(f"p{i}_idr", SELL, 800 + i)
seen = []
for t in r2.list(ENTRY):
    seen.append(t.order_id)
    r2.pop(t.pair, t.kind)
assert sorted(seen) == ["700", "701", "702", "703"], seen
assert len(r2) == 4 and all(t.kind == SELL for t in r2.list())
print("T5 OK: snapshot list() aman untuk pola 'hapus sambil iterasi'")

# --- T6: owns() — registry tracked + engine source, tanpa menyerah ke pair
r3 = OrderRegistry()
r3.add("btc_idr", ENTRY, 1234)
assert r3.owns(1234) and r3.owns("1234")
assert r3.owns(9999) is False
r3.add_engine_source(lambda: [4321, None, "8765"])
assert r3.owns(4321) and r3.owns("8765")
assert r3.owns(9999) is False
print("T6 OK: owns() gabung registry + engine source, id asing tetap asing")

# --- T7: main.py wiring — order resting grid/DCA/SM tidak dianggap yatim
assert len(m.registry._engine_sources) == 1, "main.py harus daftarkan 1 engine source"
m.S._position_states["testa_idr"] = {"tp_order_id": 9001, "sl_order_id": None}
gi = GridInstance("testb_idr", 1000, 100000)
gi.buy_order_id = 9002
gi.sell_order_id = 9003
di = DCAInstance("testc_idr")
di.tp_order_id = 9004
di.safety_orders = [{"order_id": 9005}, {"order_id": None}]
m.S.grid_mini.instances.append(gi)
m.S.dca_smart.instances["testc_idr"] = di
try:
    ids = m.S._engine_order_ids()
    assert ids == {"9001", "9002", "9003", "9004", "9005"}, ids
    for oid in ids:
        assert m.registry.owns(oid), oid
    assert m.registry.owns(9999) is False
    # order grid tanpa position: dulu masuk daftar "orphan", sekarang dilindungi
    assert m.registry.tracked_pairs() == set()
    assert m.registry.owns(gi.buy_order_id)
finally:
    m.S._position_states.pop("testa_idr", None)
    m.S.grid_mini.instances.remove(gi)
    m.S.dca_smart.instances.pop("testc_idr", None)
print("T7 OK: order resting grid/DCA/SM terlindungi dari orphan cleanup")

# --- T8: static — 3 dict lama benar-benar hilang, cleanup pakai owns()
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
for dead in ["_pending_orders", "_pending_sells", "_tp_limit_orders"]:
    assert dead not in src, f"{dead} masih dipakai di main.py"
assert "registry.owns(oid)" in src, "orphan cleanup tidak lagi cek kepemilikan order"
assert "set(_pending_orders.keys())" not in src
print("T8 OK: tidak ada sisa state dict lama; cleanup berbasis kepemilikan id")

# --- T9: ENTRY/SELL kinds dipakai main.py dengan field yang benar
t = m.registry.add("xx_idr", ENTRY, 4242, side="buy", price=100, qty=0.5,
                   amount_idr=50, is_maker=True, atr_pct=1.2, client_order_id="fma1")
assert t.side == "buy" and t.cycles == 0 and t.atr_pct == 1.2
assert m.registry.bump("xx_idr", ENTRY) == 1
m.registry.pop("xx_idr", ENTRY)
assert len(m.registry) == 0
print("T9 OK: record ENTRY lengkap (side/qty/price/amount/atr/grace)")

print("ALL ORDER-REGISTRY CHECKS PASSED")
