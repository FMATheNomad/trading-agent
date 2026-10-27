import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
config.PAPER_TRADING = True
import main as m
from state_bus import StateBus, TICK, FILL, MAX_PENDING_FILLS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def read(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


# --- T1: tick di-coalesce per pair — task WS boleh spam, state cuma lihat harga terakhir
bus = StateBus()
for i in range(1000):
    bus.publish_tick("btc_idr", 600_000_000 + i)
bus.publish_tick("eth_idr", 50_000_000)
ev = bus.drain()
assert len(ev) == 2, [e.pair for e in ev]
btc = next(e for e in ev if e.pair == "btc_idr")
assert btc.kind == TICK and btc.price == 600_000_999, btc
assert bus.pending() == 0
assert bus.drain() == [], "drain kedua harus kosong"
print("T1 OK: 1000 tick 1 pair = 1 event (harga terakhir), drain clear")

# --- T2: fill antre urut + ada batasnya (burst tidak bisa habiskan memori)
bus2 = StateBus()
for i in range(MAX_PENDING_FILLS):
    assert bus2.publish_fill(f"p{i}_idr", "sell", "FILLED") is True
assert bus2.publish_fill("banjir_idr", "sell", "FILLED") is False
assert bus2.dropped_fills == 1, bus2.dropped_fills
out = bus2.drain()
assert len(out) == MAX_PENDING_FILLS and out[0].kind == FILL
assert out[0].pair == "p0_idr" and out[-1].pair == f"p{MAX_PENDING_FILLS - 1}_idr"
print(f"T2 OK: {MAX_PENDING_FILLS} fill tertampung, sisanya drop + tercacat")


async def t3():
    # --- T3: publish bangunin wait(); drain() reset event-nya
    b = StateBus()
    awaited = asyncio.create_task(b.wait())
    await asyncio.sleep(0)
    assert not awaited.done(), "wait() harus block kalau antrean kosong"
    b.publish_tick("btc_idr", 1.0)
    await asyncio.wait_for(awaited, timeout=1)
    assert awaited.done() and not awaited.cancelled()
    b.drain()
    again = asyncio.create_task(b.wait())
    await asyncio.sleep(0)
    assert not again.done(), "drain() harus reset wake event"
    again.cancel()
    try:
        await again
    except asyncio.CancelledError:
        pass
    print("T3 OK: wait()/drain() sinkron — tidak ada event hilang atau nyangkut")


asyncio.run(t3())


async def t4():
    # --- T4: consumer = satu-satunya jalan event jadi mutasi state, dan dia
    # jalan di dalam _state_lock sehingga tidak bisa berselang-seling dengan cycle
    log = []
    orig_check = m._realtime_sltp_check
    orig_fill = m._handle_fill_event

    async def fake_check(pair, price):
        assert m.S._state_lock.locked(), "handler tick harus jalan di dalam _state_lock"
        log.append(("tick", pair, price))
        await asyncio.sleep(0.01)  # titik di mana dulu task lain bisa nyelip

    async def fake_fill(pair, side, status):
        assert m.S._state_lock.locked(), "handler fill harus jalan di dalam _state_lock"
        log.append(("fill", pair, status))

    async def fake_cycle():
        # meniru portfolio_cycle: pegang lock, mutasi, await di tengah
        async with m.S._state_lock:
            for i in range(3):
                log.append(("cycle", i, None))
                await asyncio.sleep(0.005)

    m._realtime_sltp_check = fake_check
    m._handle_fill_event = fake_fill
    consumer = asyncio.create_task(m._state_consumer())
    try:
        m.S.state_bus.publish_tick("btc_idr", 100.0)
        await asyncio.sleep(0.02)
        cycle_task = asyncio.create_task(fake_cycle())
        # spam 3 tick + 1 fill SEMENTARA cycle megang lock: tidak boleh ada
        # mutasi yang nyelip di sela-sela cycle
        for i in range(3):
            m.S.state_bus.publish_tick("eth_idr", 200.0 + i)
        m.S.state_bus.publish_fill("dogi_idr", "sell", "FILLED")
        await cycle_task
        for _ in range(100):
            if any(e[0] == "fill" for e in log):
                break
            await asyncio.sleep(0.02)
        kinds = [e[0] for e in log]
        ci = [i for i, k in enumerate(kinds) if k == "cycle"]
        assert ci and ci == list(range(ci[0], ci[0] + len(ci))), f"cycle kepotong: {kinds}"
        assert ("tick", "eth_idr", 202.0) in log, log
        assert ("fill", "dogi_idr", "FILLED") in log, log
        assert m.S.state_bus.pending() == 0, m.S.state_bus.pending()
        assert m.S.state_bus.dropped_fills == 0
    finally:
        consumer.cancel()
        m._realtime_sltp_check = orig_check
        m._handle_fill_event = orig_fill
    print(f"T4 OK: {len(log)} entri diproses serial lewat lock, cycle tidak kepotong")


asyncio.run(t4())

# --- T5: task WS benar-benar cuma publisher (zero await, nol ensure_future)
mw, pw = read("market_ws.py"), read("private_ws.py")
assert "asyncio.ensure_future(_on_tick_callback" not in mw, "market_ws masih spawn task per tick"
assert "asyncio.ensure_future(_on_fill_callback" not in pw, "private_ws masih spawn task per fill"
assert "_on_tick_callback(pair, price)" in mw and "_on_fill_callback(symbol, side, status)" in pw
assert "state_bus.publish_tick" in read("main.py")
print("T5 OK: WS loop manggil publisher sinkron, tidak men-spawn coroutine")

# --- T6: main.py — callback WS cuma nitip pesan; mutasi lewat consumer
src = read("main.py")
assert "set_on_tick(_on_ws_tick)" in src and "set_on_fill(_on_ws_fill)" in src
assert "set_on_tick(_realtime_sltp_check)" not in src
assert src.count("await _realtime_sltp_check(") == 1, "realtime check harus cuma dipanggil consumer"
assert "state_task = asyncio.create_task(_state_consumer())" in src
assert "async with S._state_lock:\n                await portfolio_cycle(client)" in src
assert "state_task.cancel()" in src
print("T6 OK: tick & fill masuk bus; hanya _state_consumer yang mengeksekusi")

# --- T7: fungsi publisher tidak menyentuh state (dicek langsung: state kosong)
before = (len(m.S.positions), dict(m.S._position_states), len(m.registry))
for i in range(50):
    m._on_ws_tick("btc_idr", 600_000_000 + i)
m._on_ws_fill("btcidr", "sell", "FILLED")
after = (len(m.S.positions), dict(m.S._position_states), len(m.registry))
assert before == after, "publisher WS tidak boleh mutasi state"
assert m.S.state_bus.pending() == 2, m.S.state_bus.pending()
m.S.state_bus.drain()
print("T7 OK: 50 tick + 1 fillpublished = state tidak berubah, antrean 2 event")

print("ALL STATE-QUEUE CHECKS PASSED")
