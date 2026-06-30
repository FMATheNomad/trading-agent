import hashlib
import hmac
import time
from urllib.parse import urlencode
import httpx
import config

def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()

async def place_order(client: httpx.AsyncClient, side: str, price: float, amount_idr: float,
                      order_type: str = "limit") -> dict:
    symbol_pair = config.PAIR
    coin = symbol_pair.split("_")[0]
    nonce = int(time.time() * 1000)

    params = {
        "method": "trade",
        "nonce": nonce,
        "pair": symbol_pair,
        "type": side,
        "price": str(int(price)),
    }

    if order_type == "market":
        params["order_type"] = "market"
        params["idr"] = str(int(amount_idr))
    else:
        coin_qty = round(amount_idr / price, 8)
        params[coin] = f"{coin_qty:.8f}"
        params["order_type"] = "limit"

    body = urlencode(params)
    sign = _sign(body, config.INDODAX_SECRET_KEY)

    headers = {
        "Key": config.INDODAX_API_KEY,
        "Sign": sign,
    }

    if config.PAPER_TRADING:
        return {
            "paper_trade": True,
            "side": side,
            "price": price,
            "amount_idr": amount_idr,
            "order_type": order_type,
            "nonce": nonce,
        }

    r = await client.post(config.INDODAX_TAPI_URL, headers=headers, data=body)
    data = r.json()
    if data.get("success") != 1:
        raise RuntimeError(f"Order failed: {data.get('error', 'unknown')}")
    return data["return"]

async def cancel_order(client: httpx.AsyncClient, order_id: int) -> dict:
    nonce = int(time.time() * 1000)
    params = {
        "method": "cancelOrder",
        "nonce": nonce,
        "pair": config.PAIR,
        "order_id": str(order_id),
        "type": "buy",
    }
    body = urlencode(params)
    sign = _sign(body, config.INDODAX_SECRET_KEY)
    headers = {"Key": config.INDODAX_API_KEY, "Sign": sign}
    r = await client.post(config.INDODAX_TAPI_URL, headers=headers, data=body)
    return r.json()

async def get_balance(client: httpx.AsyncClient) -> dict:
    nonce = int(time.time() * 1000)
    params = {"method": "getInfo", "nonce": nonce}
    body = urlencode(params)
    sign = _sign(body, config.INDODAX_SECRET_KEY)
    headers = {"Key": config.INDODAX_API_KEY, "Sign": sign}
    r = await client.post(config.INDODAX_TAPI_URL, headers=headers, data=body)
    data = r.json()
    if data.get("success") != 1:
        raise RuntimeError(f"getInfo failed: {data.get('error', 'unknown')}")
    return data["return"]

async def get_open_orders(client: httpx.AsyncClient) -> list:
    nonce = int(time.time() * 1000)
    params = {"method": "openOrders", "nonce": nonce, "pair": config.PAIR}
    body = urlencode(params)
    sign = _sign(body, config.INDODAX_SECRET_KEY)
    headers = {"Key": config.INDODAX_API_KEY, "Sign": sign}
    r = await client.post(config.INDODAX_TAPI_URL, headers=headers, data=body)
    data = r.json()
    if data.get("success") != 1:
        return []
    return data["return"].get("orders", [])
