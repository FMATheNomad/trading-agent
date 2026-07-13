# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import json
import os

DATA_DIR = os.getenv("STATE_DIR") or os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(__file__))
STATE_FILE = os.path.join(DATA_DIR, "state.json")

def _load() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save(state: dict):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"Persist save error: {e}", flush=True)

def load_entry_prices() -> dict[str, float]:
    return _load().get("entry_prices", {})

def save_entry_prices(prices: dict[str, float]):
    state = _load()
    state["entry_prices"] = {k: v for k, v in prices.items() if v > 0}
    _save(state)

def load_peak_capital() -> float | None:
    return _load().get("peak_capital")

def save_peak_capital(value: float):
    state = _load()
    state["peak_capital"] = value
    _save(state)

def load_initial_equity() -> float | None:
    return _load().get("initial_equity")

def save_initial_equity(value: float):
    state = _load()
    if "initial_equity" not in state:
        state["initial_equity"] = value
        _save(state)

def load_positions() -> list[dict]:
    return _load().get("positions", [])

def save_positions(positions: list[dict]):
    state = _load()
    clean = [
        {"pair": p["pair"], "side": p.get("side", "BUY"),
         "entry_price": p.get("entry_price", 0), "qty": p.get("qty", 0),
         "amount_idr": p.get("amount_idr", 0), "atr_pct": p.get("atr_pct"),
         "entry_time": p.get("entry_time", 0),
         "entry_mode": p.get("entry_mode", "KONSERVATIF")}
        for p in positions
    ]
    state["positions"] = clean
    _save(state)

def load_trades() -> list[dict]:
    return _load().get("trades", [])

def save_trades(trades: list[dict]):
    state = _load()
    state["trades"] = trades[-200:]
    _save(state)

def append_trade(trade: dict):
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)

def load_daily_loss_hit() -> bool:
    return _load().get("daily_loss_hit", False)

def save_daily_loss_hit(flag: bool):
    state = _load()
    state["daily_loss_hit"] = flag
    _save(state)

def load_loss_hit_date() -> str:
    return _load().get("loss_hit_date", "")

def save_loss_hit_date(date_str: str):
    state = _load()
    state["loss_hit_date"] = date_str
    _save(state)

def load_today_peak() -> float:
    return _load().get("today_peak", 0)

def save_today_peak(value: float):
    state = _load()
    state["today_peak"] = value
    _save(state)

def load_cooldown() -> dict[str, float]:
    raw = _load().get("cooldown", {})
    if not isinstance(raw, dict):
        return {}
    now = __import__("time").time()
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v > now}

def save_cooldown(data: dict[str, float]):
    state = _load()
    now = __import__("time").time()
    state["cooldown"] = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now}
    _save(state)

def load_blacklist() -> list[str]:
    return _load().get("blacklist", [])

def save_blacklist(data: set[str]):
    state = _load()
    state["blacklist"] = list(data)[:50]
    _save(state)

def load_optimizer_state() -> dict:
    raw = _load().get("optimizer_state", {})
    if not isinstance(raw, dict):
        return {"last_trade_id": 0, "last_run_time": 0}
    return {
        "last_trade_id": int(raw.get("last_trade_id", 0)),
        "last_run_time": float(raw.get("last_run_time", 0)),
    }

def save_optimizer_state(state: dict):
    s = _load()
    s["optimizer_state"] = {
        "last_trade_id": int(state.get("last_trade_id", 0)),
        "last_run_time": float(state.get("last_run_time", __import__("time").time())),
    }
    _save(s)

def load_sm_cooldown() -> dict[str, float]:
    raw = _load().get("sm_cooldown", {})
    if not isinstance(raw, dict):
        return {}
    now = __import__("time").time()
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v > now}

def save_sm_cooldown(data: dict[str, float]):
    state = _load()
    now = __import__("time").time()
    state["sm_cooldown"] = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now}
    _save(state)

def load_equity_curve() -> list[float]:
    raw = _load().get("equity_curve", [])
    if not isinstance(raw, list):
        return []
    return [float(v) for v in raw if isinstance(v, (int, float)) and v > 0]

def save_equity_curve(curve: list[float]):
    state = _load()
    trimmed = [float(v) for v in curve[-200:] if isinstance(v, (int, float)) and v > 0]
    state["equity_curve"] = trimmed
    _save(state)

def load_circuit_breaker() -> dict:
    return _load().get("circuit_breaker", {
        "consecutive_loss_days": 0,
        "last_loss_date": "",
        "triggered_at": 0,
        "active_until": 0,
    })

def save_circuit_breaker(data: dict):
    state = _load()
    state["circuit_breaker"] = data
    _save(state)
