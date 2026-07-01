import hashlib
import hmac
import time
from urllib.parse import urlencode
import httpx
import config

def _ts() -> int:
    return int(time.time() * 1000)

def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()

def _headers(body: str) -> dict:
    sign = _sign(body, config.INDODAX_SECRET_KEY)
    return {
        "Key": config.INDODAX_API_KEY,
        "Sign": sign,
        "Content-Type": "application/x-www-form-urlencoded",
    }

def fmt_coin_qty(qty: float, pair: str = "") -> str:
    s = f"{qty:.8f}".rstrip("0").rstrip(".")
    return s if s != "0" else "0"

async def place_order(client: httpx.AsyncClient, side: str, price: float, amount_idr: float,
                      pair: str | None = None, order_type: str = "limit") -> dict:
    pair = pair or config.PAIR
    coin = pair.split("_")[0]
    params = {
        "method": "trade",
        "timestamp": _ts(),
        "recvWindow": "5000",
        "pair": pair,
        "type": side,
        "price": str(int(price)),
    }

    if order_type == "market":
        params["order_type"] = "market"
        if side == "buy":
            params["idr"] = str(int(amount_idr))
        else:
            coin_qty = round(amount_idr / price, 8)
            params[coin] = fmt_coin_qty(coin_qty, pair)
    elif order_type == "maker_first":
        maker_price = int(price * (1 - config.MAKER_SLIPPAGE)) if side == "buy" else int(price * (1 + config.MAKER_SLIPPAGE))
        params["price"] = str(maker_price)
        coin_qty = round(amount_idr / maker_price, 8)
        params[coin] = fmt_coin_qty(coin_qty, pair)
        params["order_type"] = "limit"
    elif order_type == "maker":
        buy_price = int(price * 0.998)
        sell_price = int(price * 1.002)
        side_price = buy_price if side == "buy" else sell_price
        params["price"] = str(side_price)
        coin_qty = round(amount_idr / side_price, 8)
        params[coin] = fmt_coin_qty(coin_qty, pair)
        params["order_type"] = "limit"
    else:
        coin_qty = round(amount_idr / price, 8)
        params[coin] = fmt_coin_qty(coin_qty, pair)
        params["order_type"] = "limit"

    body = urlencode(params)

    if config.PAPER_TRADING:
        return {"paper_trade": True, "side": side, "pair": pair, "price": price,
                 "amount_idr": amount_idr, "order_type": order_type}

    r = await client.post(config.INDODAX_TAPI_URL, headers=_headers(body), content=body)
    data = r.json()
    if data.get("success") != 1:
        err = data.get("error", "unknown")
        if order_type == "maker_first" and "not maker" in err.lower():
            return await place_order(client, side, price, amount_idr, pair=pair, order_type="market")
        raise RuntimeError(f"Order failed {pair}: {err}")
    return data["return"]

async def cancel_order(client: httpx.AsyncClient, order_id: int, pair: str | None = None, side: str = "buy") -> dict:
    pair = pair or config.PAIR
    params = {"method": "cancelOrder", "timestamp": _ts(), "recvWindow": "5000", "pair": pair,
              "order_id": str(order_id), "type": side}
    body = urlencode(params)
    r = await client.post(config.INDODAX_TAPI_URL, headers=_headers(body), content=body)
    return r.json()

async def get_balance(client: httpx.AsyncClient) -> dict:
    params = {"method": "getInfo", "timestamp": _ts(), "recvWindow": "5000"}
    body = urlencode(params)
    r = await client.post(config.INDODAX_TAPI_URL, headers=_headers(body), content=body)
    data = r.json()
    if data.get("success") != 1:
        raise RuntimeError(f"getInfo failed: {data.get('error', 'unknown')}")
    return data["return"]

async def get_order(client: httpx.AsyncClient, order_id: int, pair: str = config.PAIR) -> dict | None:
    params = {"method": "getOrder", "timestamp": _ts(), "recvWindow": "5000", "pair": pair, "order_id": str(order_id)}
    body = urlencode(params)
    r = await client.post(config.INDODAX_TAPI_URL, headers=_headers(body), content=body)
    data = r.json()
    if data.get("success") == 1:
        return data["return"].get("order")
    return None

async def get_open_orders(client: httpx.AsyncClient, pair: str | None = None) -> list:
    pair = pair or config.PAIR
    params = {"method": "openOrders", "timestamp": _ts(), "recvWindow": "5000", "pair": pair}
    body = urlencode(params)
    r = await client.post(config.INDODAX_TAPI_URL, headers=_headers(body), content=body)
    data = r.json()
    if data.get("success") != 1:
        return []
    return data["return"].get("orders", [])



