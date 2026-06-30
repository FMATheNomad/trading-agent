import httpx
import config

PUBLIC_URL = config.INDODAX_BASE_URL
TICKER_URL = f"{PUBLIC_URL}/api/ticker/{config.PAIR}"
DEPTH_URL = f"{PUBLIC_URL}/api/depth/{config.PAIR}"
OHLCV_URL = f"{PUBLIC_URL}/tradingview/history_v2"

async def fetch_ticker(client: httpx.AsyncClient) -> dict | None:
    r = await client.get(f"{PUBLIC_URL}/api/ticker/{config.PAIR}")
    r.raise_for_status()
    data = r.json()
    t = data.get("ticker")
    if not t:
        return None
    return {
        "last": float(t["last"]),
        "buy": float(t["buy"]),
        "sell": float(t["sell"]),
        "high": float(t["high"]),
        "low": float(t["low"]),
        "vol": float(t.get(f"vol_{config.PAIR.split('_')[0]}", 0)),
        "server_time": int(t["server_time"]),
    }

async def fetch_orderbook(client: httpx.AsyncClient) -> dict | None:
    r = await client.get(f"{PUBLIC_URL}/api/depth/{config.PAIR}")
    r.raise_for_status()
    data = r.json()
    return {
        "bids": [[float(p), float(q)] for p, q in data.get("buy", [])],
        "asks": [[float(p), float(q)] for p, q in data.get("sell", [])],
    }

async def fetch_ohlcv(client: httpx.AsyncClient, tf: int = 15, limit: int = 200) -> list[dict]:
    import time
    now = int(time.time())
    start = now - (limit * tf * 60)
    r = await client.get(
        OHLCV_URL,
        params={"from": start, "to": now, "tf": tf, "symbol": config.SYMBOL},
    )
    r.raise_for_status()
    raw = r.json()
    return [{k.lower(): v for k, v in bar.items()} for bar in raw]
