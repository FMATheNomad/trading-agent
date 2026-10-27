import asyncio, os, re, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import httpx
import config
config.PAPER_TRADING = True
config.INDODAX_SECRET_KEY = "secret"
config.INDODAX_API_KEY = "key"
config.USE_TAPI_V2 = False  # these suites pin the legacy transport
import executor

async def main():
    # --- T1: signed (POST) requests must NOT touch the network in paper mode.
    # Public GET metadata refresh is allowed.
    def boom(request):
        if request.method == "POST":
            raise AssertionError(f"HTTP LEAK in paper mode: {request.url}")
        return httpx.Response(200, json=[])
    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        d = await executor.tapi_request(c, {"method": "getInfo"})
        assert d["success"] == 0 and "PAPER" in d["error"], d
        o = await executor.place_order(c, "buy", 100000, 50000, pair="btc_idr", order_type="market")
        assert o.get("paper_trade") is True, o
        r = await executor.get_order(c, 123, pair="btc_idr")
        assert r is None, r
        canc = await executor.cancel_order(c, 123, pair="btc_idr")
        assert canc["success"] == 0, canc
    print("T1 OK: paper mode = zero live HTTP from every gateway function")

    # --- T2: with PAPER off, gateway posts again (guard is not a permanent mute)
    config.PAPER_TRADING = False
    hits = []
    def ok(request):
        hits.append(request)
        return httpx.Response(200, json={"success": 1, "return": {"orders": []}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as c:
        data = await executor.get_open_orders(c, pair="btc_idr")
        assert len(hits) == 1 and data == []
    print("T2 OK: live mode still sends signed requests")

asyncio.run(main())

# --- T3: static check — the old paper risk bypasses must be gone everywhere
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "main.py")


def decision_code() -> str:
    """main.py + engine/*.py = seluruh jalur keputusan bot."""
    import glob
    parts = [open(SRC, encoding="utf-8").read()]
    for p in sorted(glob.glob(os.path.join(ROOT, "engine", "*.py"))):
        parts.append(open(p, encoding="utf-8").read())
    return "\n".join(parts)


src = decision_code()
forbidden = [
    r"daily_limit = None",
    r"config\.MAX_OPEN_POSITIONS = 12",
    r"config\.MAX_POSITION_PCT_PER_ASSET = 0\.50",
    r"config\.REGIME_STABILITY_CYCLES = 2",
    r"check_portfolio_stop\(total_equity\) and not config\.PAPER_TRADING",
    r"PAPER MODE: daily loss flag cleared",
]
for pat in forbidden:
    m = re.search(pat, src)
    assert not m, f"paper risk bypass still present: {pat}"
print("T3 OK: all 6 paper risk bypasses removed from main.py + engine/")
print("ALL PAPER-PARITY CHECKS PASSED")
