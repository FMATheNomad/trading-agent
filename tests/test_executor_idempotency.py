import asyncio, sys, time, urllib.parse
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import httpx
import config
config.PAPER_TRADING = False
config.INDODAX_TAPI_URL = "https://mock.tapi"
config.INDODAX_SECRET_KEY = "secret"
config.INDODAX_API_KEY = "key"
config.USE_TAPI_V2 = False  # these suites pin the legacy transport
import executor
# Pin the pair-rules TTL cache so place_order's lazy refresh doesn't emit
# extra GETs through our handlers; default rules apply.
executor._PAIR_RULES = {"_pin": {}}
executor._PAIR_RULES_TS = time.time()

calls = []
RAISE_ONCE = {"armed": True}

def handler(request: httpx.Request) -> httpx.Response:
    body = dict(urllib.parse.parse_qsl(request.content.decode()))
    calls.append(body)
    m = body.get("method")
    if m == "trade":
        coid = body.get("client_order_id", "")
        assert coid.startswith("fma") and len(coid) <= 36, f"bad coid {coid}"
        if RAISE_ONCE["armed"]:
            RAISE_ONCE["armed"] = False
            raise httpx.ReadTimeout("simulated network timeout", request=request)
        return httpx.Response(200, json={"success": 1, "return": {"order_id": 111, "receive_btc": "0.0002"}})
    if m == "getOrderByClientOrderId":
        return httpx.Response(200, json={"success": 1, "return": {"order": {
            "order_id": "999", "status": "idle", "price": "117000000",
            "receive_idr": "0", "client_order_id": body["client_order_id"]}}})
    return httpx.Response(200, json={"success": 0, "error": "unexpected " + str(m)})

async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # --- CASE 1: submit times out but order actually landed -> recovered, NO second trade POST
        calls.clear()
        order = await executor.place_order(c, "buy", 117000000, 25000, pair="btc_idr", order_type="market")
        trade_posts = [b for b in calls if b["method"] == "trade"]
        lookups = [b for b in calls if b["method"] == "getOrderByClientOrderId"]
        assert len(trade_posts) == 1, f"double order! {len(trade_posts)} trade posts"
        assert len(lookups) == 1, "recovery lookup missing"
        assert order["order_id"] == "999", order
        print("CASE1 OK: timeout -> recovered by client_order_id, only 1 trade POST")

        # --- CASE 2: normal success path, fresh ids per call
        calls.clear()
        o2 = await executor.place_order(c, "buy", 117000000, 25000, pair="btc_idr", order_type="market")
        o3 = await executor.place_order(c, "sell", 117000000, 25000, pair="btc_idr", order_type="market", qty=0.0002)
        assert o2["order_id"] == 111 and o3["order_id"] == 111
        coids = [b["client_order_id"] for b in calls if b["method"] == "trade"]
        assert len(set(coids)) == 2, "client_order_id not unique"
        print("CASE2 OK: unique client_order_id per order")

        # --- CASE 3: maker_first "not maker" reject -> retry must use NEW id
        calls.clear()
        def handler3(request):
            body = dict(urllib.parse.parse_qsl(request.content.decode()))
            calls.append(body)
            if body["method"] == "trade":
                if len([x for x in calls if x["method"] == "trade"]) == 1:
                    return httpx.Response(200, json={"success": 0, "error": "Order cancelled because it's not maker."})
                return httpx.Response(200, json={"success": 1, "return": {"order_id": 222}})
            return httpx.Response(200, json={"success": 1, "return": {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler3)) as c3:
            r = await executor.place_order(c3, "buy", 117000000, 25000, pair="btc_idr", order_type="maker_first")
            trades = [b for b in calls if b["method"] == "trade"]
            assert len(trades) == 2 and trades[0]["client_order_id"] != trades[1]["client_order_id"], \
                "retry reused client_order_id (would hit 'already exists')"
            assert r["order_id"] == 222
        print("CASE3 OK: maker_first retry uses fresh client_order_id")

        # --- CASE 4: timeout and order never landed -> raise, never blind-retry
        calls.clear()
        def handler4(request):
            body = dict(urllib.parse.parse_qsl(request.content.decode()))
            calls.append(body)
            if body["method"] == "trade":
                raise httpx.ConnectTimeout("dead", request=request)
            return httpx.Response(200, json={"success": 0, "error": "not found"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler4)) as c4:
            try:
                await executor.place_order(c4, "buy", 117000000, 25000, pair="btc_idr", order_type="market")
                raise AssertionError("should have raised")
            except RuntimeError as e:
                assert "uncertain" in str(e)
        trades4 = [b for b in calls if b["method"] == "trade"]
        assert len(trades4) == 1, f"blind retry happened: {len(trades4)}"
        print("CASE4 OK: lost order -> raises 'uncertain', zero blind retries")

        # --- CASE 5: 429 too_many_requests -> backoff + retry (safe: 429 never created an order)
        calls5 = []
        def handler5(request):
            body = dict(urllib.parse.parse_qsl(request.content.decode()))
            calls5.append(body)
            if len(calls5) == 1:
                return httpx.Response(429, json={
                    "success": 0, "error_code": "too_many_requests",
                    "error": "You have exceeded the rate limit for this action. Please try again in 1 seconds."})
            return httpx.Response(200, json={"success": 1, "return": {"orders": []}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler5)) as c5:
            data = await executor.get_open_orders(c5, pair="btc_idr")
            assert len(calls5) == 2, f"expected 1 retry after 429, got {len(calls5)} calls"
            assert data == [], data
        print("CASE5 OK: 429 -> parsed 'try again in 1 seconds', retried once, succeeded")

        # --- CASE 6: token bucket actually spaces bursty cancels
        calls6 = []
        def handler6(request):
            calls6.append(time.monotonic())
            return httpx.Response(200, json={"success": 1, "return": {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler6)) as c6:
            t0 = time.monotonic()
            await asyncio.gather(*[executor.cancel_order(c6, 1000 + i, pair="btc_idr") for i in range(20)])
            span = time.monotonic() - t0
            assert len(calls6) == 20
            assert span >= 0.25, f"rate limiter didn't throttle 20 cancels (span {span:.2f}s)"
        print(f"CASE6 OK: 20 cancels through bucket took {span:.2f}s (capacity 15, rate 15/s)")

asyncio.run(main())
print("ALL EXECUTOR IDEMPOTENCY CHECKS PASSED")
