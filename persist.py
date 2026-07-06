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
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
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

def load_cooldown() -> dict[str, float]:
    return _load().get("cooldown", {})

def save_cooldown(cooldown: dict[str, float]):
    state = _load()
    state["cooldown"] = cooldown
    _save(state)

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
    return _load().get("cooldown", {})

def save_cooldown(data: dict[str, float]):
    state = _load()
    now = __import__("time").time()
    state["cooldown"] = {k: v for k, v in data.items() if v > now}
    _save(state)

def load_blacklist() -> list[str]:
    return _load().get("blacklist", [])

def save_blacklist(data: set[str]):
    state = _load()
    state["blacklist"] = list(data)[:50]
    _save(state)
