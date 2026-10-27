# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import httpx
import config
from executor import tapi_request

COUNTDOWN_MS = config.DEADMAN_COUNTDOWN_MS


async def _countdown(client: httpx.AsyncClient, pairs: str | None, countdown_ms: int) -> bool:
    params = {
        "pair": pairs or config.PAIR,
        "countdownTime": str(countdown_ms),
        "recvWindow": "5000",
    }
    try:
        result = await tapi_request(
            client, params, kind="deadman",
            url=f"{config.INDODAX_TAPI_URL}/countdownCancelAll",
            content_type="text/plain",
        )
        return result.get("success") == 1
    except Exception as e:
        print(f"Deadman refresh failed: {e}", flush=True)
        return False


async def refresh_deadman(client: httpx.AsyncClient, pairs: str | None = None) -> bool:
    return await _countdown(client, pairs, COUNTDOWN_MS)


async def cancel_deadman(client: httpx.AsyncClient, pairs: str | None = None) -> bool:
    return await _countdown(client, pairs, 0)
