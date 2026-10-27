import asyncio, hashlib, hmac, os, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
import config

config.PAPER_TRADING = False
config.USE_TAPI_V2 = True
config.INDODAX_V2_API_KEY = "V2KEY"
config.INDODAX_V2_SECRET_KEY = "v2secret"
config.INDODAX_TAPI_V2_URL = "https://mock.indodax"
config.INDODAX_TAPI_URL = "https://mock-tapi"
config.INDODAX_API_KEY = "V1KEY"
config.INDODAX_SECRET_KEY = "v1secret"
import executor

executor._PAIR_RULES = {"btc_idr": {"pricescale": 1000.0, "quantity_increment": 1e-8,
                                    "min_base": 10_000.0, "min_traded": 0.0, "tradable": True}}
executor._PAIR_RULES_TS = time.time()

rec = []


def _record(request: httpx.Request) -> str:
    url = str(request.url)
    if request.method in ("GET", "DELETE"):
        payload = url.split("?", 1)[1] if "?" in url else ""
    else:
        payload = request.content.decode()
    rec.append({"m": request.method, "url": url, "payload": payload,
                "params": dict(urllib.parse.parse_qsl(payload)),
                "headers": dict(request.headers)})
    return payload


def _ok_sign(request: httpx.Request) -> bool:
    payload = _record(request)
    return request.headers.get("Sign") == hmac.new(
        config.INDODAX_V2_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _resp(json_body, status=200):
    return httpx.Response(status, json=json_body)


ORDER_CREATED = {"symbol": "BTCIDR", "orderId": 6423, "clientOrderId": "x",
                 "side": "BUY", "type": "LIMIT", "price": "117000000",
                 "origQty": "0.00021367", "executedQty": "0.0",
                 "fullOrderId": "btcidr-limit-6423"}


async def main():
    # --- T1: MARKET BUY -> POST /api/v2/order with quoteOrderQty, no quantity
    def h1(request):
        if not _ok_sign(request):
            return _resp({"code": -1022, "msg": "bad sign"})
        return _resp(ORDER_CREATED)
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h1)) as c:
        o = await executor.place_order(c, "buy", 117_000_000, 25_000, pair="btc_idr", order_type="market")
    r = rec[0]
    assert r["m"] == "POST" and r["url"] == "https://mock.indodax/api/v2/order", r["url"]
    assert r["params"]["symbol"] == "BTCIDR" and r["params"]["side"] == "BUY" and r["params"]["type"] == "MARKET"
    assert r["params"]["quoteOrderQty"] == "25000" and "quantity" not in r["params"], r["params"]
    assert r["params"]["selfTradePreventionMode"] == "EXPIRE_TAKER"
    assert r["params"]["newClientOrderId"].startswith("fma") and len(r["params"]["newClientOrderId"]) <= 36
    assert r["headers"]["x-apikey"] == "V2KEY" and r["headers"]["accept"] == "application/json"
    assert r["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert "timestamp" in r["params"] and "recvWindow" in r["params"]
    assert o["order_id"] == "6423" and o["pair"] == "btc_idr" and o["type"] == "buy", o
    print("T1 OK: MARKET BUY -> v2 quoteOrderQty, HMAC-SHA256 signed body, legacy-shaped result")

    # --- T2: LIMIT SELL -> price+quantity, tick/lot applied; MOC only for maker_first
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h1)) as c:
        await executor.place_order(c, "sell", 117_345_678, 0, pair="btc_idr", order_type="limit", qty=0.000213679)
        await executor.place_order(c, "buy", 117_000_000, 25_000, pair="btc_idr", order_type="maker_first")
    a, b = rec[0], rec[1]
    assert a["params"]["type"] == "LIMIT" and a["params"]["price"] == "117345000", a["params"]
    assert a["params"]["quantity"] == "0.00021367" and "quoteOrderQty" not in a["params"]
    assert "timeInForce" not in a["params"]
    assert b["params"]["timeInForce"] == "MOC" and b["params"]["side"] == "BUY", b["params"]
    print("T2 OK: LIMIT uses price+quantity (tick 117345000, lot 0.00021367); maker_first -> timeInForce=MOC")

    # --- T3: error envelope mapping (-2010 rejected) and -1021 clock hint
    def h3(request):
        _record(request)
        return _resp({"code": -2010, "msg": "Order was rejected: insufficient balance"})
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h3)) as c:
        try:
            await executor.place_order(c, "buy", 117_000_000, 25_000, pair="btc_idr", order_type="market")
            raise AssertionError("should raise")
        except RuntimeError as e:
            assert "insufficient balance" in str(e), e
        try:
            await executor.get_balance(c)
            raise AssertionError("get_balance should raise")
        except RuntimeError as e:
            assert "insufficient balance" in str(e), e
    print("T3 OK: v2 {code,msg} errors become explicit RuntimeErrors (no silent success)")

    def h3b(request):
        _record(request)
        return _resp({"code": -1021, "msg": "timestamp outside recvWindow"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(h3b)) as c:
        data = await executor.get_open_orders(c, pair="btc_idr")
        assert data == [], data
        try:
            await executor.get_balance(c)
            raise AssertionError("should raise")
        except RuntimeError as e:
            assert "system clock" in str(e), e
    print("T3b OK: -1021 surfaces a clock hint instead of a cryptic failure")

    # --- T4: 429 / -1003 -> backoff + retry (never an order-side effect)
    calls4 = []

    def h4(request):
        payload = _record(request)
        calls4.append(payload)
        if len(calls4) == 1:
            return _resp({"code": -1003, "msg": "Too many requests. Please try again in 1 seconds."}, status=429)
        return _resp([{"symbol": "BTCIDR", "orderId": 1, "side": "SELL", "price": "117000000",
                       "origQty": "0.0002", "executedQty": "0", "status": "NEW", "type": "LIMIT",
                       "time": 1723442692520, "clientOrderId": "fma1"}])
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h4)) as c:
        orders = await executor.get_open_orders(c, pair="btc_idr")
    assert len(calls4) == 2, calls4
    assert orders[0]["status"] == "open" and orders[0]["order_id"] == "1", orders
    assert orders[0]["submit_time"] == 1723442692
    assert float(orders[0]["remain_btc"]) == 0.0002 and orders[0]["remain_rp"] > 0, orders[0]
    print("T4 OK: -1003/429 retried after parsed backoff; NEW status -> 'open', remain>0 while open")

    # --- T5: filled and cancelled orders report remain 0 (callers key on it)
    def h5(request):
        _record(request)
        return _resp({"symbol": "BTCIDR", "orderId": 7, "side": "SELL", "price": "117000000",
                      "origQty": "0.0002", "executedQty": "0.0002", "status": "FILLED", "type": "LIMIT",
                      "time": 1723442692520, "clientOrderId": "fma7"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(h5)) as c:
        oi = await executor.get_order(c, 7, pair="btc_idr")
    assert oi["status"] == "filled" and float(oi["remain_btc"]) == 0 and oi["remain_rp"] == 0, oi
    assert oi["receive_btc"] == "0.00020000"
    print("T5 OK: FILLED -> status 'filled', remain_btc/remain_rp 0, receive_btc set")

    # --- T6: account mapping to v1 balance/balance_hold shape
    def h6(request):
        _record(request)
        return _resp({"canTrade": True, "canWithdraw": False, "accountType": "individual", "uid": 1,
                      "balances": [{"asset": "IDR", "free": "300000.00", "locked": "0.00"},
                                   {"asset": "BTC", "free": "0.00021367", "locked": "0.00010000"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(h6)) as c:
        info = await executor.get_balance(c)
    assert float(info["balance"]["idr"]) == 300000 and float(info["balance"]["btc"]) == 0.00021367, info
    assert float(info["balance_hold"]["btc"]) == 0.0001 and info["can_trade"] is True
    print("T6 OK: /api/v2/account -> balance / balance_hold maps with lowercase assets")

    # --- T7: openOrders without pair groups by pair (v1 shape main.py expects)
    def h7(request):
        _record(request)
        return _resp([
            {"symbol": "BTCIDR", "orderId": 11, "side": "SELL", "price": "117000000", "origQty": "0.0002",
             "executedQty": "0", "status": "NEW", "type": "LIMIT", "time": 1, "clientOrderId": "a"},
            {"symbol": "DOGEIDR", "orderId": 12, "side": "BUY", "price": "1000", "origQty": "50",
             "executedQty": "0", "status": "NEW", "type": "LIMIT", "time": 2, "clientOrderId": "b"},
        ])
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h7)) as c:
        grouped = await executor.get_open_orders(c)
    assert set(grouped) == {"btc_idr", "doge_idr"}, grouped
    assert grouped["doge_idr"][0]["type"] == "buy" and grouped["doge_idr"][0]["order_id"] == "12"
    assert "symbol" not in rec[0]["params"], rec[0]["params"]
    print("T7 OK: pair-less openOrders -> per-pair dict for orphan cleanup")

    # --- T8: timeout recovery uses symbol + origClientOrderId, single POST
    def h8(request):
        payload = _record(request)
        p = dict(urllib.parse.parse_qsl(payload))
        if request.method == "POST":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        assert p["symbol"] == "BTCIDR" and p["origClientOrderId"].startswith("fma"), p
        o = dict(ORDER_CREATED)
        o["status"] = "NEW"
        return _resp(o)
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h8)) as c:
        o = await executor.place_order(c, "buy", 117_000_000, 25_000, pair="btc_idr", order_type="market")
    posts = [x for x in rec if x["m"] == "POST"]
    gets = [x for x in rec if x["m"] == "GET"]
    assert len(posts) == 1, f"double order: {len(posts)} POSTs"
    assert len(gets) == 1 and o["order_id"] == "6423", (gets, o)
    print("T8 OK: v2 timeout recovered via GET /api/v2/order?symbol&origClientOrderId, no re-POST")

    # --- T9: MOC killed -> single deeper GTC retry with a fresh client_order_id
    def h9(request):
        payload = _record(request)
        p = dict(urllib.parse.parse_qsl(payload))
        if request.method == "POST":
            if "timeInForce" in p:
                return _resp({"code": -2010, "msg": "Order cancelled because it's not maker."})
            return _resp(ORDER_CREATED)
        return _resp(ORDER_CREATED)
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h9)) as c:
        r = await executor.place_order(c, "buy", 117_000_000, 25_000, pair="btc_idr", order_type="maker_first")
    trades = [x for x in rec if x["m"] == "POST"]
    assert len(trades) == 2 and trades[0]["params"]["newClientOrderId"] != trades[1]["params"]["newClientOrderId"]
    assert "timeInForce" not in trades[1]["params"], "retry must be allowed to cross"
    assert r["order_id"] == "6423"
    print("T9 OK: MOC rejection retried once, deeper, with a fresh client_order_id")

    # --- T10: cancel + get_order dispatch (DELETE with orderId, no body)
    def h10(request):
        _record(request)
        o = dict(ORDER_CREATED)
        o.update({"status": "CANCELLED", "executedQty": "0"})
        return _resp(o)
    rec.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h10)) as c:
        res = await executor.cancel_order(c, 6423, pair="btc_idr", side="sell")
        res2 = await executor.cancel_order(c, "fmadeadbeef", pair="btc_idr")
    assert rec[0]["m"] == "DELETE" and rec[0]["params"]["orderId"] == "6423" and rec[0]["params"]["symbol"] == "BTCIDR"
    assert rec[1]["params"]["origClientOrderId"] == "fmadeadbeef", rec[1]["params"]
    assert res["success"] == 1 and res["return"]["status"] == "cancelled"
    assert res2["success"] == 1
    print("T10 OK: cancel -> DELETE /api/v2/order (orderId or origClientOrderId), mapped result")

    # --- T11: paper mode still blocks every v2 endpoint
    config.PAPER_TRADING = True
    hits = []

    def h11(request):
        hits.append(str(request.url))
        raise AssertionError(f"paper mode sent HTTP: {request.url}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(h11)) as c:
        pr = await executor.place_order(c, "buy", 117_000_000, 25_000, pair="btc_idr", order_type="market")
        assert pr.get("paper_trade") is True
        try:
            await executor.get_balance(c)
            raise AssertionError("paper get_balance should raise")
        except RuntimeError as e:
            assert "PAPER_TRADING" in str(e), e
        assert await executor.get_open_orders(c, pair="btc_idr") == []
        assert (await executor.cancel_order(c, 1, pair="btc_idr")).get("success") == 0
        assert await executor.get_order(c, 1, pair="btc_idr") is None
        assert await executor.get_order_by_client_order_id(c, "fmax", pair="btc_idr") is None
    assert hits == [], hits
    config.PAPER_TRADING = False
    print("T11 OK: paper mode issues zero v2 HTTP calls across all six endpoints")

    # --- T12: legacy-only gateway refuses to sign without v1 keys (deadman path)
    config.INDODAX_API_KEY = ""
    config.INDODAX_SECRET_KEY = ""
    hits.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(h11)) as c:
        d = await executor.tapi_request(c, {"method": "getInfo"})
    assert d.get("success") == 0 and "legacy TAPI credentials" in d.get("error", ""), d
    assert hits == [], hits
    print("T12 OK: legacy transport fails loudly without v1 keys instead of sending a bad signature")

asyncio.run(main())
print("ALL TAPI V2 CHECKS PASSED")
