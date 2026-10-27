# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import asyncio
import hashlib
import hmac
import json
import httpx
import websockets
from urllib.parse import urlencode
import config

_shutdown = False
_on_fill_callback = None

def set_on_fill(callback):
    global _on_fill_callback
    _on_fill_callback = callback

async def _generate_token(key: str, secret: str) -> tuple[str, str] | None:
    body = urlencode({"client": "tapi", "tapi_key": key})
    sign = hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://indodax.com/api/private_ws/v1/generate_token",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Sign": sign},
                content=body,
            )
            data = r.json()
            if data.get("success") == 1:
                ret = data["return"]
                return ret["connToken"], ret["channel"]
    except Exception as e:
        print(f"PWS token error: {e}", flush=True)
    return None


async def _token_any_credential() -> tuple[str, str] | None:
    # The token endpoint has no v2 counterpart; try the v2 key first (it is
    # still a TAPI key) and fall back to the legacy one.
    creds = []
    if config.USE_TAPI_V2:
        creds.append((config.INDODAX_V2_API_KEY, config.INDODAX_V2_SECRET_KEY))
    creds.append((config.INDODAX_API_KEY, config.INDODAX_SECRET_KEY))
    for key, secret in creds:
        if not key or not secret:
            continue
        tok = await _generate_token(key, secret)
        if tok:
            return tok
    return None

async def private_ws_loop():
    global _shutdown
    while not _shutdown:
        try:
            tok = await _token_any_credential()
            if not tok:
                await asyncio.sleep(60)
                continue
            token, channel = tok
            async with websockets.connect(config.WS_PRIVATE_URL, ping_interval=30) as ws:
                await ws.send(json.dumps({"connect": {"token": token}, "id": 1}))
                auth = json.loads(await ws.recv())
                if "error" in auth:
                    print(f"PWS auth error: {auth['error']}", flush=True)
                    await asyncio.sleep(60)
                    continue
                await ws.send(json.dumps({"subscribe": {"channel": channel}, "id": 2}))
                sub = json.loads(await ws.recv())
                if "error" in sub:
                    print(f"PWS subscribe error: {sub['error']}", flush=True)
                    continue
                print(f"Private WS connected & subscribed", flush=True)

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        push = msg.get("push", {}).get("pub", {})
                        for event in push.get("data", []):
                            order = event.get("order", {})
                            status = order.get("status", "")
                            if status in ("FILLED", "FILL", "DONE", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"):
                                symbol = order.get('symbol', '?')
                                side = order.get('side', '?')
                                qty = order.get('executedQty', '?')
                                price = order.get('price', '?')
                                print(f"PWS: {symbol} {side} {qty} @ {price} → {status}", flush=True)
                                if _on_fill_callback:
                                    _on_fill_callback(symbol, side, status)
                    except Exception:
                        pass
        except Exception as e:
            print(f"PWS error: {e}, reconnecting in 10s...", flush=True)
            await asyncio.sleep(10)

def stop():
    global _shutdown
    _shutdown = True
