import hashlib
import hmac
import time
from urllib.parse import urlencode
import httpx
import config

COUNTDOWN_MS = 300_000

def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()

async def refresh_deadman(client: httpx.AsyncClient) -> bool:
    ts = int(time.time() * 1000)
    params = {
        "pair": config.PAIR,
        "countdownTime": str(COUNTDOWN_MS),
        "timestamp": str(ts),
        "recvWindow": "5000",
    }
    body = urlencode(params)
    sign = _sign(body, config.INDODAX_SECRET_KEY)
    headers = {
        "Key": config.INDODAX_API_KEY,
        "Sign": sign,
        "Content-Type": "text/plain",
    }
    try:
        r = await client.post(
            f"{config.INDODAX_TAPI_URL}/countdownCancelAll",
            headers=headers,
            data=body,
        )
        result = r.json()
        return result.get("success") == 1
    except Exception:
        return False

async def cancel_deadman(client: httpx.AsyncClient) -> bool:
    ts = int(time.time() * 1000)
    params = {
        "pair": config.PAIR,
        "countdownTime": "0",
        "timestamp": str(ts),
        "recvWindow": "5000",
    }
    body = urlencode(params)
    sign = _sign(body, config.INDODAX_SECRET_KEY)
    headers = {
        "Key": config.INDODAX_API_KEY,
        "Sign": sign,
        "Content-Type": "text/plain",
    }
    try:
        r = await client.post(
            f"{config.INDODAX_TAPI_URL}/countdownCancelAll",
            headers=headers,
            data=body,
        )
        result = r.json()
        return result.get("success") == 1
    except Exception:
        return False
