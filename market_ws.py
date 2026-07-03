# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import asyncio
import json
import websockets
import config

LIVE_TICKERS: dict[str, dict] = {}
_ws = None
_on_tick_callback = None

def set_on_tick(callback):
    global _on_tick_callback
    _on_tick_callback = callback

async def market_ws_loop():
    global _ws
    while not _shutdown:
        try:
            async with websockets.connect(config.WS_MARKET_URL, ping_interval=30) as ws:
                _ws = ws
                await ws.send(json.dumps({
                    "params": {"token": config.WS_MARKET_TOKEN},
                    "id": 1,
                }))
                resp = await ws.recv()
                print(f"Market WS auth: {json.loads(resp).get('result', {}).get('client', '?')[:12]}...", flush=True)

                await ws.send(json.dumps({
                    "method": 1, "params": {"channel": "market:summary-24h"}, "id": 2,
                }))
                resp2 = await ws.recv()
                print("Market WS subscribed to market:summary-24h", flush=True)

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        data = msg.get("result", {}).get("data", {}).get("data", [])
                        for entry in data:
                            if len(entry) >= 8:
                                pair_id = entry[0]
                                pair = pair_id.replace("idr", "_idr") if not pair_id.endswith("_idr") else pair_id
                                price = float(entry[2])
                                LIVE_TICKERS[pair] = {
                                    "last": price,
                                    "low_24h": float(entry[3]),
                                    "high_24h": float(entry[4]),
                                    "open_24h": float(entry[5]),
                                    "vol_idr": float(entry[6]),
                                    "vol_coin": float(entry[7]),
                                    "change_24h": ((price - float(entry[5])) / float(entry[5]) * 100) if float(entry[5]) else 0,
                                }
                                if _on_tick_callback:
                                    asyncio.ensure_future(_on_tick_callback(pair, price))
                    except Exception:
                        pass
        except Exception as e:
            print(f"Market WS error: {e}, reconnecting in 5s...", flush=True)
            await asyncio.sleep(5)

_shutdown = False

def stop():
    global _shutdown
    _shutdown = True
