import hashlib
import hmac
import time
from urllib.parse import urlencode
import httpx
import config

COUNTDOWN_MS = config.DEADMAN_COUNTDOWN_MS

def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()

def _headers(body: str) -> dict:
    return {
        "Key": config.INDODAX_API_KEY,
        "Sign": _sign(body, config.INDODAX_SECRET_KEY),
        "Content-Type": "text/plain",
    }

async def refresh_deadman(client: httpx.AsyncClient, pairs: str | None = None) -> bool:
    ts = int(time.time() * 1000)
    params = {
        "pair": pairs or config.PAIR,
        "countdownTime": str(COUNTDOWN_MS),
        "timestamp": str(ts),
        "recvWindow": "5000",
    }
    body = urlencode(params)
    try:
        r = await client.post(
            f"{config.INDODAX_TAPI_URL}/countdownCancelAll",
            headers=_headers(body),
            content=body,
        )
        result = r.json()
        return result.get("success") == 1
    except Exception as e:
        print(f"Deadman refresh failed: {e}", flush=True)
        return False

async def cancel_deadman(client: httpx.AsyncClient, pairs: str | None = None) -> bool:
    ts = int(time.time() * 1000)
    params = {
        "pair": pairs or config.PAIR,
        "countdownTime": "0",
        "timestamp": str(ts),
        "recvWindow": "5000",
    }
    body = urlencode(params)
    try:
        r = await client.post(
            f"{config.INDODAX_TAPI_URL}/countdownCancelAll",
            headers=_headers(body),
            content=body,
        )
        return r.json().get("success") == 1
    except Exception:
        return False
