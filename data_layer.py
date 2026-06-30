import time
import httpx
import config

PUBLIC_URL = config.INDODAX_BASE_URL

async def fetch_ticker(client: httpx.AsyncClient, pair: str | None = None) -> dict | None:
    pair = pair or config.PAIR
    r = await client.get(f"{PUBLIC_URL}/api/ticker/{pair}")
    r.raise_for_status()
    data = r.json()
    t = data.get("ticker")
    if not t:
        return None
    base = pair.split("_")[0]
    return {
        "pair": pair,
        "last": float(t["last"]),
        "buy": float(t["buy"]),
        "sell": float(t["sell"]),
        "high": float(t["high"]),
        "low": float(t["low"]),
        "vol": float(t.get(f"vol_{base}", 0)),
        "vol_idr": float(t.get("vol_idr", 0)),
        "server_time": int(t["server_time"]),
    }

async def fetch_orderbook(client: httpx.AsyncClient, pair: str | None = None) -> dict | None:
    pair = pair or config.PAIR
    r = await client.get(f"{PUBLIC_URL}/api/depth/{pair}")
    r.raise_for_status()
    data = r.json()
    return {
        "bids": [[float(p), float(q)] for p, q in data.get("buy", [])],
        "asks": [[float(p), float(q)] for p, q in data.get("sell", [])],
    }

async def fetch_ohlcv(client: httpx.AsyncClient, pair: str | None = None,
                       tf: int = 60, limit: int = 100) -> list[dict]:
    pair = pair or config.PAIR
    symbol = pair.replace("_", "").upper()
    now = int(time.time())
    start = now - (limit * tf * 60)
    r = await client.get(
        f"{PUBLIC_URL}/tradingview/history_v2",
        params={"from": start, "to": now, "tf": tf, "symbol": symbol},
    )
    r.raise_for_status()
    raw = r.json()
    return [{k.lower(): v for k, v in bar.items()} for bar in raw]

async def fetch_all_pairs(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{PUBLIC_URL}/api/pairs")
    r.raise_for_status()
    return r.json()

async def fetch_all_tickers(client: httpx.AsyncClient) -> dict:
    r = await client.get(f"{PUBLIC_URL}/api/ticker_all")
    r.raise_for_status()
    data = r.json()
    result = {}
    for pair_id, t in data.get("tickers", {}).items():
        base = pair_id.split("_")[0]
        result[pair_id] = {
            "pair": pair_id,
            "last": float(t["last"]),
            "buy": float(t["buy"]),
            "sell": float(t["sell"]),
            "high": float(t["high"]),
            "low": float(t["low"]),
            "vol": float(t.get(f"vol_{base}", 0)),
            "vol_idr": float(t.get("vol_idr", 0)),
            "server_time": int(t["server_time"]),
        }
    return result

async def fetch_ohlcv_both(client: httpx.AsyncClient, pair: str) -> tuple[list[dict], list[dict]]:
    ohlcv_1h = await fetch_ohlcv(client, pair=pair, tf=60, limit=100)
    ohlcv_4h = await fetch_ohlcv(client, pair=pair, tf=240, limit=100)
    return ohlcv_1h or [], ohlcv_4h or []

async def fetch_viable_pairs(client: httpx.AsyncClient) -> list[dict]:
    pairs = await fetch_all_pairs(client)
    tickers = await fetch_all_tickers(client)
    candidates = []
    for p in pairs:
        pid = p.get("ticker_id", "")
        if not pid.endswith("_idr"):
            continue
        t = tickers.get(pid)
        if not t:
            continue
        vol_idr = t.get("vol_idr", 0)
        if vol_idr < config.MIN_24H_VOLUME_IDR:
            continue
        candidates.append({
            "pair": pid,
            "traded": p.get("traded_currency", ""),
            "price_precision": p.get("price_precision", 1000),
            "trade_min_base": p.get("trade_min_base_currency", 50000),
            "trade_min_traded": p.get("trade_min_traded_currency", 0.0001),
            "ticker": t,
            "_vol": vol_idr,
        })
    candidates.sort(key=lambda x: x["_vol"], reverse=True)
    return candidates[:config.MAX_SCAN_PAIRS]
