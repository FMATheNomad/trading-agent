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

async def _generate_token() -> tuple[str, str] | None:
    if not config.INDODAX_API_KEY or not config.INDODAX_SECRET_KEY:
        return None
    body = urlencode({"client": "tapi", "tapi_key": config.INDODAX_API_KEY})
    sign = hmac.new(config.INDODAX_SECRET_KEY.encode(), body.encode(), hashlib.sha512).hexdigest()
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

async def private_ws_loop():
    global _shutdown
    while not _shutdown:
        try:
            tok = await _generate_token()
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
                            if status in ("FILL", "DONE", "CANCELLED", "REJECTED"):
                                print(f"PWS: {order.get('symbol')} {order.get('side')} "
                                      f"{order.get('executedQty')} @ {order.get('price')} → {status}", flush=True)
                    except Exception:
                        pass
        except Exception as e:
            print(f"PWS error: {e}, reconnecting in 10s...", flush=True)
            await asyncio.sleep(10)

def stop():
    global _shutdown
    _shutdown = True
