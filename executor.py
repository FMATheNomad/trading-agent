# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""executor.py — single gateway to Indodax TAPI (v2 REST + v1 legacy).

All signed requests leave through here: HMAC signing, per-kind rate limiting
and 429 backoff live in one place. The public API (place_order, get_balance,
...) is version-agnostic — v2 responses are mapped back to the legacy v1
field names and statuses, so call sites never branch on the API version.

Docs: v1 trade 20 req/s per pair, cancel 30 req/s, deadman 10 req/10s per IP.
v2 create 20 req/s per pair, cancel 30 req/s, most reads 300 req/min per IP.
The deadman switch (countdownCancelAll) has no v2 equivalent, so it keeps
using the legacy endpoint with the v1 key.
"""

import asyncio
import hashlib
import hmac
import math
import random
import re
import time
import uuid
from urllib.parse import urlencode
import httpx
import config


def _ts() -> int:
    return int(time.time() * 1000)


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()


def _headers(body: str, content_type: str = "application/x-www-form-urlencoded") -> dict:
    return {
        "Key": config.INDODAX_API_KEY,
        "Sign": _sign(body, config.INDODAX_SECRET_KEY),
        "Content-Type": content_type,
    }


def _v2_sign(payload: str) -> str:
    return hmac.new(config.INDODAX_V2_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _new_client_order_id() -> str:
    # TAPI v1 and v2: alphanumeric + _ -, max 36 chars. "fma" + 32-hex = 35.
    return f"fma{uuid.uuid4().hex}"


# --- Pair trading rules (public /api/pairs metadata) -----------------------
# pricescale = price tick in IDR, quantity_increment = lot size,
# trade_min_* = exchange minimums. Orders must conform or the exchange
# rejects them (silently, after we already spent the rate budget).

DEFAULT_RULES = {
    "pricescale": 1.0,
    "quantity_increment": 1e-8,
    "min_base": 10_000.0,
    "min_traded": 0.0,
    "tradable": True,
}

_PAIR_RULES: dict[str, dict] = {}
_PAIR_RULES_TS = 0.0
_PAIR_RULES_TTL = 6 * 3600
_PAIR_RULES_LOCK: asyncio.Lock | None = None


async def refresh_pair_rules(client: httpx.AsyncClient, force: bool = False) -> int:
    """Fetch /api/pairs metadata into _PAIR_RULES (TTL-cached).
    Never raises: on failure existing (or default) rules keep the bot running."""
    global _PAIR_RULES, _PAIR_RULES_TS, _PAIR_RULES_LOCK
    if _PAIR_RULES_LOCK is None:
        _PAIR_RULES_LOCK = asyncio.Lock()
    now = time.time()
    if not force and _PAIR_RULES and now - _PAIR_RULES_TS < _PAIR_RULES_TTL:
        return len(_PAIR_RULES)
    async with _PAIR_RULES_LOCK:
        now = time.time()
        if not force and _PAIR_RULES and now - _PAIR_RULES_TS < _PAIR_RULES_TTL:
            return len(_PAIR_RULES)
        try:
            r = await client.get(f"{config.INDODAX_BASE_URL}/api/pairs")
            raw = r.json()
            if not isinstance(raw, list):
                return len(_PAIR_RULES)
            rules = {}
            for p in raw:
                if not isinstance(p, dict):
                    continue
                pid = p.get("ticker_id")
                if not pid:
                    continue
                ps = float(p.get("pricescale") or 1)
                qi = float(p.get("quantity_increment") or 1e-8)
                rules[pid] = {
                    "pricescale": ps if ps > 0 else 1.0,
                    "quantity_increment": qi if qi > 0 else 1e-8,
                    "min_base": float(p.get("trade_min_base_currency") or 10000),
                    "min_traded": float(p.get("trade_min_traded_currency") or 0),
                    "tradable": not (int(p.get("is_maintenance") or 0) or int(p.get("is_market_suspended") or 0)),
                }
            if rules:
                _PAIR_RULES = rules
                _PAIR_RULES_TS = time.time()
        except Exception as e:
            print(f"  pair rules refresh failed: {e}", flush=True)
        return len(_PAIR_RULES)


def pair_rules(pair: str) -> dict:
    return _PAIR_RULES.get(pair, DEFAULT_RULES)


def quantize_price(price: float, pair: str, side: str) -> int:
    """Floor price to the pair's tick. Floor is the aggressive direction for
    this bot (buy limits sit below market, sells must cross for SL)."""
    ps = pair_rules(pair)["pricescale"]
    if ps <= 1:
        return int(round(price))
    return int(math.floor(price / ps) * ps)


def fmt_coin_qty(qty: float, pair: str = "") -> str:
    """Truncate qty to the pair's lot size and render without trailing zeros."""
    inc = pair_rules(pair)["quantity_increment"] if pair else 1e-8
    steps = math.floor(qty / inc + 1e-9)
    val = steps * inc
    if val <= 0:
        return "0"
    if inc >= 1:
        return str(int(val))
    decimals = max(0, -int(math.floor(math.log10(inc) + 1e-9)))
    s = f"{val:.{decimals}f}".rstrip("0").rstrip(".")
    return s if s != "0" else "0"


def _validate_size(pair: str, side: str, price: float, amount_idr: float,
                   coin_qty: float) -> None:
    r = pair_rules(pair)
    if not r["tradable"]:
        raise RuntimeError(f"{pair} suspended/in maintenance")
    min_base = r["min_base"]
    if side == "buy":
        invest = amount_idr if amount_idr > 0 else price * coin_qty
        if invest < min_base:
            raise ValueError(f"{pair} buy Rp{invest:,.0f} < min Rp{min_base:,.0f}")
    if coin_qty > 0 and coin_qty < r["min_traded"]:
        raise ValueError(f"{pair} qty {coin_qty} < min_traded {r['min_traded']}")
    if side == "sell" and price * coin_qty < min_base:
        raise ValueError(f"{pair} sell Rp{price * coin_qty:,.0f} < min Rp{min_base:,.0f}")


# --- Order spec: bot intent -> exchange-neutral numbers ---------------------

def _order_spec(pair: str, side: str, price: float, amount_idr: float,
                order_type: str, qty: float | None) -> dict:
    """Normalize (side, order_type, amounts) into one LIMIT/MARKET spec.

    `idr` is set only where the exchange sizes the order in IDR; `qty` is the
    base-asset size. v1 sends idr for market/maker buys, v2 requires quantity
    for every order except MARKET BUY, so both versions derive from here.
    """
    p = float(price)
    if p <= 0:
        raise ValueError(f"{pair} price must be > 0, got {price}")
    if order_type == "market":
        if side == "buy":
            return {"otype": "MARKET", "price": int(p), "qty": None,
                    "idr": int(amount_idr), "moc": False}
        return {"otype": "MARKET", "price": int(p),
                "qty": qty if qty is not None else round(amount_idr / p, 8),
                "idr": None, "moc": False}
    if order_type == "maker_first":
        sl = config.MAKER_SLIPPAGE
        mp = p * (1 - sl) if side == "buy" else p * (1 + sl)
        q = qty if qty is not None else round(amount_idr / (mp if side == "buy" else p), 8)
        return {"otype": "LIMIT", "price": int(mp), "qty": q, "idr": None, "moc": True}
    if order_type == "maker":
        mp = p * (0.998 if side == "buy" else 1.002)
        q = qty if qty is not None else round(amount_idr / mp, 8)
        return {"otype": "LIMIT", "price": int(mp), "qty": q,
                "idr": int(amount_idr) if side == "buy" else None, "moc": False}
    q = qty if qty is not None else round(amount_idr / p, 8)
    return {"otype": "LIMIT", "price": int(p), "qty": q, "idr": None, "moc": False}


# --- Rate limiting ----------------------------------------------------------

class _TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int):
        self._rate = rate_per_sec
        self._cap = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            await asyncio.sleep(wait)


# Conservative budgets vs documented limits (see module docstring). v2 reads
# are capped per minute per IP, so they get their own slower bucket.
_LIMITERS: dict[str, _TokenBucket] = {
    "trade": _TokenBucket(8.0, 8),
    "cancel": _TokenBucket(15.0, 15),
    "deadman": _TokenBucket(0.5, 5),
    "read": _TokenBucket(8.0, 8),
    "v2read": _TokenBucket(4.0, 8),
}


def _kind_for(params: dict) -> str:
    m = str(params.get("method", params.get("countdownTime") and "countdown" or "read"))
    if m == "trade":
        return "trade"
    if m in ("cancelOrder", "cancelByClientOrderId"):
        return "cancel"
    if "countdownTime" in params:
        return "deadman"
    return "read"


def _is_rate_limited(resp: httpx.Response, data) -> tuple[bool, float]:
    if not isinstance(data, dict):
        data = {}
    if resp.status_code == 429 or data.get("error_code") == "too_many_requests" or data.get("code") == -1003:
        msg = str(data.get("error") or data.get("message") or data.get("msg") or "")
        m = re.search(r"in (\d+(?:\.\d+)?) second", msg)
        delay = float(m.group(1)) if m else 5.0
        return True, delay + random.uniform(0.0, 0.5)
    return False, 0.0


# --- v1 legacy transport ----------------------------------------------------

async def tapi_request(client: httpx.AsyncClient, params: dict, *,
                       kind: str | None = None, url: str | None = None,
                       content_type: str = "application/x-www-form-urlencoded",
                       max_attempts: int = 3) -> dict:
    """Signed legacy (v1) TAPI POST with per-kind rate limiting and 429 backoff.
    Retries only on explicit rate-limit rejects, which never created an order."""
    if config.PAPER_TRADING:
        # Safety invariant: paper mode never sends a live signed request,
        # no matter which call site forgets its own PAPER_TRADING gate.
        return {"success": 0, "error": "PAPER_TRADING: live request blocked", "return": {}}
    if not (config.INDODAX_API_KEY and config.INDODAX_SECRET_KEY):
        # Legacy-only endpoints (deadman switch) still need the v1 key.
        return {"success": 0, "error": "legacy TAPI credentials missing", "return": {}}
    p = dict(params)
    p.setdefault("timestamp", str(_ts()))
    p.setdefault("recvWindow", "5000")
    kind = kind or _kind_for(p)
    bucket = _LIMITERS[kind]
    target = url or config.INDODAX_TAPI_URL
    body = urlencode(p)
    last_data: dict = {}
    for attempt in range(max_attempts):
        await bucket.acquire()
        r = await client.post(target, headers=_headers(body, content_type), content=body)
        try:
            data = r.json()
        except ValueError:
            data = {}
        last_data = data
        limited, delay = _is_rate_limited(r, data)
        if not limited:
            return data
        if attempt < max_attempts - 1:
            print(f"  TAPI 429 ({kind}): backoff {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)
    return last_data


# --- v2 transport -----------------------------------------------------------

_V2_STATUS = {"NEW": "open", "PARTIALLY_FILLED": "open", "FILLED": "filled",
              "CANCELLED": "cancelled", "REJECTED": "rejected"}


def _symbol_of(pair: str) -> str:
    return str(pair).replace("_", "").upper()


def _pair_of(symbol: str) -> str:
    s = str(symbol or "").lower()
    if s.endswith("idr"):
        s = s[:-3]
    return f"{s}_idr"


def _coin_of(symbol: str) -> str:
    return _pair_of(symbol).split("_")[0]


def _v2_ok(data) -> bool:
    """v2 has no success envelope; errors are {"code": -N, "msg": ...}."""
    return not (isinstance(data, dict) and "code" in data)


def _v2_envelope(data) -> dict:
    if _v2_ok(data):
        return {"success": 1, "return": data}
    msg = str(data.get("msg") or "unknown")
    if data.get("code") == -1021:
        msg += " (check system clock: v2 rejects timestamps outside recvWindow)"
    return {"success": 0, "error": msg, "error_code": data.get("code")}


async def _v2_request(client: httpx.AsyncClient, method: str, path: str,
                      params: dict, *, kind: str, max_attempts: int = 3) -> dict:
    """Signed TAPI v2 call, returned in the legacy {"success","return"} envelope.
    Same paper-mode invariant and 429 backoff as the v1 gateway."""
    if config.PAPER_TRADING:
        return {"success": 0, "error": "PAPER_TRADING: live request blocked", "return": {}}
    if not (config.INDODAX_V2_API_KEY and config.INDODAX_V2_SECRET_KEY):
        return {"success": 0, "error": "TAPI v2 credentials missing", "return": {}}
    p = dict(params)
    p.setdefault("timestamp", str(_ts()))
    p.setdefault("recvWindow", "5000")
    payload = urlencode(p)
    headers = {
        "Accept": "application/json",
        "X-APIKEY": config.INDODAX_V2_API_KEY,
        "Sign": _v2_sign(payload),
    }
    base = f"{config.INDODAX_TAPI_V2_URL}{path}"
    url = f"{base}?{payload}" if method in ("GET", "DELETE") else base
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    bucket = _LIMITERS[kind]
    last: dict = {"success": 0, "error": "no response", "return": {}}
    for attempt in range(max_attempts):
        await bucket.acquire()
        if method == "POST":
            r = await client.post(url, headers=headers, content=payload)
        elif method == "DELETE":
            r = await client.request("DELETE", url, headers=headers)
        else:
            r = await client.get(url, headers=headers)
        try:
            data = r.json()
        except ValueError:
            data = {}
        limited, delay = _is_rate_limited(r, data)
        if not limited:
            return _v2_envelope(data)
        last = _v2_envelope(data)
        if attempt < max_attempts - 1:
            print(f"  TAPI v2 429 ({kind}): backoff {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)
    return last


def _map_v2_order(o: dict) -> dict:
    """v2 order -> legacy order dict (v1 field names and lowercase statuses)."""
    coin = _coin_of(o.get("symbol", ""))
    orig = float(o.get("origQty") or 0)
    done = float(o.get("executedQty") or 0)
    unfilled = max(orig - done, 0.0)
    price = float(o.get("price") or 0)
    side = str(o.get("side", "")).lower()
    status = _V2_STATUS.get(str(o.get("status", "")).upper(), str(o.get("status", "")).lower())
    # MARKET orders carry no price, so the IDR remainder falls back to the
    # unfilled base qty: callers only test remain > 0 to mean "still open".
    remain_rp = unfilled * price if price > 0 else unfilled
    return {
        "order_id": str(o.get("orderId", "")),
        "client_order_id": o.get("clientOrderId") or o.get("origClientOrderId") or "",
        "pair": _pair_of(o.get("symbol", "")),
        "symbol": o.get("symbol", ""),
        "type": side,
        "side": side,
        "order_type": str(o.get("type", "")).lower(),
        "status": status,
        "price": o.get("price", "0"),
        "submit_time": int(int(o.get("time") or 0) / 1000),
        "full_order_id": o.get("fullOrderId", ""),
        f"order_{coin}": f"{orig:.8f}",
        f"remain_{coin}": f"{unfilled:.8f}",
        f"receive_{coin}": f"{done:.8f}",
        "remain_rp": remain_rp,
        "remain_idr": remain_rp,
        "receive_idr": done * price if price > 0 else 0,
        "receive_rp": done * price if price > 0 else 0,
        "spend_rp": done * price if price > 0 else 0,
    }


def _map_v2_account(d: dict) -> dict:
    bal: dict[str, str] = {}
    hold: dict[str, str] = {}
    for b in d.get("balances") or []:
        if not isinstance(b, dict):
            continue
        asset = str(b.get("asset") or "").lower()
        if not asset:
            continue
        bal[asset] = str(b.get("free", "0"))
        hold[asset] = str(b.get("locked", "0"))
    return {"server_time": _ts() // 1000, "balance": bal, "balance_hold": hold,
            "can_trade": d.get("canTrade"), "can_withdraw": d.get("canWithdraw"),
            "account_type": d.get("accountType", "")}


def _v2_order_params(symbol: str, side: str, spec: dict, qty_str: str | None, coid: str) -> dict:
    p: dict = {"symbol": symbol, "side": side.upper()}
    if spec["otype"] == "MARKET":
        p["type"] = "MARKET"
        if spec["idr"] is not None:
            p["quoteOrderQty"] = str(spec["idr"])
        else:
            p["quantity"] = qty_str
    else:
        p["type"] = "LIMIT"
        p["price"] = str(spec["price"])
        p["quantity"] = qty_str
        if spec.get("moc"):
            # Maker-or-cancel: the exchange kills the order instead of
            # crossing, which replaces v1's "not maker" error dance.
            p["timeInForce"] = "MOC"
    p["newClientOrderId"] = coid
    # Never cancel our own resting order (a TP/SL) to satisfy a new one.
    p["selfTradePreventionMode"] = "EXPIRE_TAKER"
    return p


# --- Public API (version-agnostic) ------------------------------------------

async def place_order(client: httpx.AsyncClient, side: str, price: float, amount_idr: float,
                      pair: str | None = None, order_type: str = "limit",
                      qty: float | None = None) -> dict:
    pair = pair or config.PAIR
    coin = pair.split("_")[0]
    await refresh_pair_rules(client)
    spec = _order_spec(pair, side, price, amount_idr, order_type, qty)

    # Conform to exchange tick/lot rules and reject sub-minimum orders before
    # they cost a rate-limited API slot. Runs in paper mode too (parity).
    spec["price"] = quantize_price(float(spec["price"]), pair, side)
    qty_str = fmt_coin_qty(spec["qty"], pair) if spec["qty"] is not None else None
    sent_qty = float(qty_str) if qty_str is not None else 0.0
    _validate_size(pair, side, spec["price"], spec["idr"] or amount_idr, sent_qty)

    if config.PAPER_TRADING:
        return {"paper_trade": True, "side": side, "pair": pair, "price": price,
                "amount_idr": amount_idr, "order_type": order_type,
                "client_order_id": _new_client_order_id()}

    if config.USE_TAPI_V2:
        return await _place_order_v2(client, pair, side, price, spec, qty_str, order_type)
    return await _place_order_v1(client, pair, coin, side, price, spec, qty_str, order_type)


async def _place_order_v2(client, pair, side, price, spec, qty_str, order_type) -> dict:
    coid = _new_client_order_id()
    params = _v2_order_params(_symbol_of(pair), side, spec, qty_str, coid)
    try:
        data = await _v2_request(client, "POST", "/api/v2/order", params, kind="trade")
    except (httpx.TimeoutException, httpx.TransportError) as e:
        recovered = await _recover_by_client_order_id(client, coid, pair=pair)
        if recovered is None:
            raise RuntimeError(f"Order submit uncertain {pair}, no order found for {coid}: {e}") from e
        return recovered

    if data.get("success") != 1:
        err = str(data.get("error", "unknown"))
        if order_type == "maker_first" and _looks_not_maker(err):
            return await _retry_deeper_v2(client, pair, side, price, spec, order_type, err)
        raise RuntimeError(f"Order failed {pair}: {err}")

    mapped = _map_v2_order(data["return"] or {})
    # Create responses omit status; a MOC order that was killed for crossing
    # still returns 200, so re-read it before treating it as resting.
    if spec.get("moc") and not mapped["status"]:
        fresh = await get_order(client, mapped["order_id"], pair=pair)
        if fresh:
            mapped = fresh
    if order_type == "maker_first" and mapped["status"] == "cancelled":
        return await _retry_deeper_v2(client, pair, side, price, spec, order_type, "not maker (MOC killed)")
    return mapped


def _looks_not_maker(err: str) -> bool:
    e = err.lower()
    return "not maker" in e or "would cross" in e or "order would immediately match" in e


async def _retry_deeper_v2(client, pair, side, price, spec, order_type, err) -> dict:
    adj = 0.997 if side == "buy" else 1.003
    spec2 = dict(spec)
    spec2["price"] = quantize_price(int(price * adj), pair, side)
    spec2["moc"] = False  # second attempt is allowed to cross
    qty2 = fmt_coin_qty(spec2["qty"], pair) if spec2["qty"] is not None else None
    if spec2["otype"] == "MARKET" and qty2 is not None:
        spec2["qty"] = float(qty2)
    coid2 = _new_client_order_id()  # rejected order keeps its id; retry needs a fresh one
    params2 = _v2_order_params(_symbol_of(pair), side, spec2, qty2, coid2)
    try:
        d2 = await _v2_request(client, "POST", "/api/v2/order", params2, kind="trade")
    except (httpx.TimeoutException, httpx.TransportError) as e2:
        recovered = await _recover_by_client_order_id(client, coid2, pair=pair)
        if recovered is not None:
            return recovered
        raise RuntimeError(f"Order failed {pair}: {err} (retry uncertain: {e2})") from e2
    if d2.get("success") == 1:
        return _map_v2_order(d2["return"] or {})
    raise RuntimeError(f"Order failed {pair}: {err} (retry: {d2.get('error', '?')})")


async def _place_order_v1(client, pair, coin, side, price, spec, qty_str, order_type) -> dict:
    params = {
        "method": "trade",
        "recvWindow": "5000",
        "pair": pair,
        "type": side,
        "price": str(spec["price"]),
        "client_order_id": _new_client_order_id(),
        "timestamp": str(_ts()),
        "order_type": "market" if spec["otype"] == "MARKET" else "limit",
    }
    if spec["idr"] is not None and spec["otype"] == "MARKET" and side == "buy":
        params["idr"] = str(spec["idr"])
    elif spec["idr"] is not None and spec["otype"] == "LIMIT" and side == "buy":
        params["idr"] = str(spec["idr"])
    else:
        params[coin] = qty_str
    try:
        data = await tapi_request(client, params)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        # Request may still have been accepted by the exchange. Resolve via
        # client_order_id instead of blind-retrying, which risks a double order.
        recovered = await _recover_by_client_order_id(client, params["client_order_id"])
        if recovered is None:
            raise RuntimeError(f"Order submit uncertain {pair}, no order found for {params['client_order_id']}: {e}") from e
        return recovered

    if data.get("success") != 1:
        err = str(data.get("error", "unknown"))
        if order_type == "maker_first" and _looks_not_maker(err):
            adj = 0.997 if side == "buy" else 1.003
            params2 = dict(params)
            params2["price"] = str(quantize_price(int(price * adj), pair, side))
            params2["client_order_id"] = _new_client_order_id()
            params2["timestamp"] = str(_ts())
            try:
                d2 = await tapi_request(client, params2)
            except (httpx.TimeoutException, httpx.TransportError) as e2:
                recovered2 = await _recover_by_client_order_id(client, params2["client_order_id"])
                if recovered2 is not None:
                    return recovered2
                raise RuntimeError(f"Order failed {pair}: {err} (retry uncertain: {e2})") from e2
            if d2.get("success") == 1:
                return d2["return"]
            raise RuntimeError(f"Order failed {pair}: {err} (retry: {d2.get('error', '?')})")
        raise RuntimeError(f"Order failed {pair}: {err}")
    return data["return"]


async def _recover_by_client_order_id(client: httpx.AsyncClient, client_order_id: str,
                                      pair: str | None = None) -> dict | None:
    """After a network timeout, check whether the exchange actually accepted
    the order. Returns the order dict, or None if it never landed.
    Raises if the order landed but was cancelled/rejected (treat as failure)."""
    for attempt in range(3):
        try:
            order = await get_order_by_client_order_id(client, client_order_id, pair=pair)
        except (httpx.TimeoutException, httpx.TransportError):
            order = None
        if order:
            status = str(order.get("status", "")).lower()
            if status in ("cancelled", "rejected"):
                raise RuntimeError(f"Order landed but {status} (client_order_id={client_order_id})")
            return order
        if attempt < 2:
            await asyncio.sleep(1.5)
    return None


async def cancel_order(client: httpx.AsyncClient, order_id: int, pair: str | None = None,
                       side: str = "buy") -> dict:
    pair = pair or config.PAIR
    if config.USE_TAPI_V2:
        oid = str(order_id).strip()
        key = "origClientOrderId" if not oid.isdigit() else "orderId"
        data = await _v2_request(client, "DELETE", "/api/v2/order",
                                 {"symbol": _symbol_of(pair), key: oid}, kind="cancel")
        if data.get("success") == 1:
            return {"success": 1, "return": _map_v2_order(data["return"] or {})}
        return data
    params = {"method": "cancelOrder", "recvWindow": "5000", "pair": pair,
              "order_id": str(order_id), "type": side}
    return await tapi_request(client, params)


async def get_balance(client: httpx.AsyncClient) -> dict:
    if config.USE_TAPI_V2:
        data = await _v2_request(client, "GET", "/api/v2/account", {}, kind="v2read")
        if data.get("success") != 1:
            raise RuntimeError(f"account failed: {data.get('error', 'unknown')}")
        return _map_v2_account(data["return"] or {})
    data = await tapi_request(client, {"method": "getInfo", "recvWindow": "5000"})
    if data.get("success") != 1:
        raise RuntimeError(f"getInfo failed: {data.get('error', 'unknown')}")
    return data["return"]


async def get_order(client: httpx.AsyncClient, order_id: int, pair: str = config.PAIR) -> dict | None:
    if config.USE_TAPI_V2:
        oid = str(order_id).strip()
        key = "origClientOrderId" if not oid.isdigit() else "orderId"
        data = await _v2_request(client, "GET", "/api/v2/order",
                                 {"symbol": _symbol_of(pair), key: oid}, kind="v2read")
        if data.get("success") == 1:
            return _map_v2_order(data["return"] or {})
        return None
    data = await tapi_request(client, {"method": "getOrder", "recvWindow": "5000",
                                       "pair": pair, "order_id": str(order_id)})
    if data.get("success") == 1:
        return data["return"].get("order")
    return None


async def get_order_by_client_order_id(client: httpx.AsyncClient, client_order_id: str,
                                       pair: str | None = None) -> dict | None:
    if config.USE_TAPI_V2:
        if pair:
            data = await _v2_request(client, "GET", "/api/v2/order",
                                     {"symbol": _symbol_of(pair),
                                      "origClientOrderId": client_order_id}, kind="v2read")
            if data.get("success") == 1:
                return _map_v2_order(data["return"] or {})
            return None
        # v2 needs a symbol; fall back to scanning open orders across pairs.
        for o in await get_open_orders(client) or []:
            if o.get("client_order_id") == client_order_id:
                return o
        return None
    data = await tapi_request(client, {"method": "getOrderByClientOrderId", "recvWindow": "5000",
                                       "client_order_id": client_order_id})
    if data.get("success") == 1:
        return data["return"].get("order")
    return None


async def get_open_orders(client: httpx.AsyncClient, pair: str | None = None):
    """pair given -> list of orders; pair omitted -> dict keyed by pair (v1 shape)."""
    if config.USE_TAPI_V2:
        params = {"symbol": _symbol_of(pair)} if pair else {}
        data = await _v2_request(client, "GET", "/api/v2/openOrders", params, kind="v2read")
        if data.get("success") != 1:
            return [] if pair else {}
        raw = data["return"]
        orders = [_map_v2_order(o) for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
        if pair:
            return orders
        grouped: dict[str, list] = {}
        for o in orders:
            grouped.setdefault(o["pair"], []).append(o)
        return grouped
    params = {"method": "openOrders", "recvWindow": "5000"}
    if pair:
        params["pair"] = pair
    data = await tapi_request(client, params)
    if data.get("success") != 1:
        return []
    return data["return"].get("orders", [])
