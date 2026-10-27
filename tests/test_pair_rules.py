import asyncio, math, sys, time, urllib.parse
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import httpx
import config
config.PAPER_TRADING = False
config.INDODAX_TAPI_URL = "https://mock.tapi"
config.INDODAX_SECRET_KEY = "secret"
config.INDODAX_API_KEY = "key"
config.USE_TAPI_V2 = False  # these suites pin the legacy transport
import executor

SAMPLE_PAIRS = [
    {"ticker_id": "btc_idr", "pricescale": 1000, "quantity_increment": 1e-08,
     "trade_min_base_currency": 10000, "trade_min_traded_currency": 6.85e-06,
     "is_maintenance": 0, "is_market_suspended": 0},
    {"ticker_id": "usdt_idr", "pricescale": 1, "quantity_increment": 1,
     "trade_min_base_currency": 10000, "trade_min_traded_currency": 295787.97,
     "is_maintenance": 0, "is_market_suspended": 0},
    {"ticker_id": "abc_idr", "pricescale": 10, "quantity_increment": 1,
     "trade_min_base_currency": 10000, "trade_min_traded_currency": 1,
     "is_maintenance": 0, "is_market_suspended": 1},
    {"ticker_id": "small_idr", "pricescale": 10, "quantity_increment": 0.001,
     "trade_min_base_currency": 50000, "trade_min_traded_currency": 0.01,
     "is_maintenance": 0, "is_market_suspended": 0},
]

posts, gets = [], 0

def handler(request: httpx.Request) -> httpx.Response:
    global gets
    if request.method == "GET":
        gets += 1
        return httpx.Response(200, json=SAMPLE_PAIRS)
    body = dict(urllib.parse.parse_qsl(request.content.decode()))
    posts.append(body)
    return httpx.Response(200, json={"success": 1, "return": {"order_id": 555}})

async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # --- T1: refresh + TTL cache
        executor._PAIR_RULES.clear(); executor._PAIR_RULES_TS = 0
        n = await executor.refresh_pair_rules(c)
        assert n == 4 and gets == 1, (n, gets)
        await executor.refresh_pair_rules(c)
        assert gets == 1, "TTL cache missed -> refetched"
        assert executor.pair_rules("btc_idr")["pricescale"] == 1000
        assert "nope_idr" not in executor._PAIR_RULES
        assert executor.pair_rules("nope_idr") is executor.DEFAULT_RULES
        print("T1 OK: /api/pairs cached, unknown pair falls back to defaults")

        # --- T2: price quantized to tick (floor)
        p = executor.quantize_price(117345678, "btc_idr", "buy")
        assert p == 117345000 and p % 1000 == 0, p
        assert executor.quantize_price(16499.7, "usdt_idr", "sell") == 16500
        assert executor.quantize_price(1234.5, "small_idr", "sell") == 1230
        assert executor.quantize_price(117345678, "unknown_idr", "buy") == 117345678
        print("T2 OK: pricescale flooring incl. ps=1 and unknown-pair passthrough")

        # --- T3: qty truncated to lot size
        assert executor.fmt_coin_qty(0.000213049, "btc_idr") == "0.00021304"
        assert executor.fmt_coin_qty(12345.678, "usdt_idr") == "12345", executor.fmt_coin_qty(12345.678, "usdt_idr")
        assert executor.fmt_coin_qty(1.234567, "small_idr") == "1.234"
        assert executor.fmt_coin_qty(0.5, "usdt_idr") == "0"  # below 1-unit lot
        print("T3 OK: quantity_increment truncation")

        # --- T4: live order carries quantized price + valid qty
        posts.clear()
        await executor.place_order(c, "buy", 117345678, 25000, pair="btc_idr", order_type="limit")
        b = posts[-1]
        assert b["price"] == "117345000", b["price"]
        assert float(b["btc"]) * 117345000 >= 10000
        print(f"T4 OK: limit buy POSTed price={b['price']} qty={b['btc']}")

        # --- T5: rejections BEFORE any HTTP POST
        posts.clear()
        for kwargs, exc in [
            (dict(pair="btc_idr", order_type="market", side="buy", price=117e6, amount=5000), ValueError),
            (dict(pair="small_idr", order_type="market", side="buy", price=5000, amount=20000), ValueError),
            (dict(pair="abc_idr", order_type="market", side="buy", price=5000, amount=20000), RuntimeError),
        ]:
            try:
                await executor.place_order(c, kwargs["side"], kwargs["price"], kwargs["amount"],
                                           pair=kwargs["pair"], order_type=kwargs["order_type"])
                raise AssertionError(f"should reject {kwargs['pair']}")
            except exc as e:
                assert not posts, f"POST happened before rejection: {posts}"
        # sell below min_traded
        try:
            await executor.place_order(c, "sell", 117_000_000, 0, pair="btc_idr", order_type="market", qty=1e-9)
            raise AssertionError("should reject dust qty")
        except ValueError:
            pass
        # sell whose proceeds are dust (< min_base) — 1e-5 BTC would truncate to 0 qty
        try:
            await executor.place_order(c, "sell", 100_000, 0, pair="btc_idr", order_type="limit", qty=0.05)
            raise AssertionError("should reject dust sell value")
        except ValueError:
            pass
        assert not posts
        print("T5 OK: sub-min + suspended orders rejected with zero API calls")

        # --- T6: suspended pair
        posts.clear()
        try:
            await executor.place_order(c, "buy", 5000, 25000, pair="abc_idr", order_type="market")
            raise AssertionError("suspended pair accepted")
        except RuntimeError as e:
            assert "suspended" in str(e).lower() or "maintenance" in str(e).lower()
        assert not posts
        print("T6 OK: maintenance/suspended pairs blocked")

asyncio.run(main())
print("ALL PAIR-RULES CHECKS PASSED")
