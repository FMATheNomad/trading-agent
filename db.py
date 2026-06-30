import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "trades.db")

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL,
                qty REAL,
                amount_idr REAL,
                order_type TEXT,
                status TEXT DEFAULT 'simulated',
                pnl REAL,
                reason TEXT,
                paper_trade INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_positions (
                pair TEXT, side TEXT, entry_price REAL, qty REAL,
                amount_idr REAL, atr_pct REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                raw_signal TEXT,
                llm_decision TEXT,
                llm_reasoning TEXT,
                executed INTEGER DEFAULT 0
            )
        """)

def log_trade(side: str, price: float, qty: float, amount_idr: float,
              order_type: str = "limit", status: str = "simulated",
              pnl: float | None = None, reason: str = ""):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, side, price, qty, amount_idr, order_type, status, pnl, reason, paper_trade) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), side, price, qty,
             amount_idr, order_type, status, pnl, reason, 1),
        )

def log_decision(raw_signal: str, llm_decision: str, llm_reasoning: str, executed: bool = False):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO decisions (timestamp, raw_signal, llm_decision, llm_reasoning, executed) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), raw_signal, llm_decision, llm_reasoning, int(executed)),
        )

def get_recent_trades(limit: int = 3) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT side, price, qty, status, pnl, reason FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"side": r[0], "price": r[1], "qty": r[2], "status": r[3], "pnl": r[4], "reason": r[5]}
        for r in rows
    ]

def save_positions(positions: list[dict]):
    with _conn() as conn:
        conn.execute("DELETE FROM bot_positions")
        for p in positions:
            conn.execute(
                "INSERT INTO bot_positions (pair, side, entry_price, qty, amount_idr, atr_pct) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (p["pair"], p["side"], p.get("entry_price", 0), p.get("qty", 0),
                 p.get("amount_idr", 0), p.get("atr_pct")),
            )

def load_positions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT pair, side, entry_price, qty, amount_idr, atr_pct FROM bot_positions"
        ).fetchall()
    return [
        {"pair": r[0], "side": r[1], "entry_price": r[2], "qty": r[3],
         "amount_idr": r[4], "atr_pct": r[5]}
        for r in rows
    ]

def get_trade_count_today() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp LIKE ?", (f"{today}%",)
        ).fetchone()
    return row[0] if row else 0
