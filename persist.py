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

def load_positions() -> list[dict]:
    return _load().get("positions", [])

def save_positions(positions: list[dict]):
    state = _load()
    clean = [
        {"pair": p["pair"], "side": p.get("side", "BUY"),
         "entry_price": p.get("entry_price", 0), "qty": p.get("qty", 0),
         "amount_idr": p.get("amount_idr", 0), "atr_pct": p.get("atr_pct"),
         "entry_time": p.get("entry_time", 0)}
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
