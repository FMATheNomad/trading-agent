# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import sqlite3
import os
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))

DATA_DIR = os.getenv("STATE_DIR") or os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "trades.db")

def _conn():
    return sqlite3.connect(DB_PATH)

_write_count = 0

def _checkpoint():
    try:
        with _conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

def init_db():
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
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
    _checkpoint()

def log_trade(side: str, price: float, qty: float, amount_idr: float,
              order_type: str = "limit", status: str = "simulated",
              pnl: float | None = None, reason: str = ""):
    global _write_count
    with _conn() as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, side, price, qty, amount_idr, order_type, status, pnl, reason, paper_trade) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(WIB).isoformat(), side, price, qty,
             amount_idr, order_type, status, pnl, reason, 1),
        )
    _write_count += 1
    if _write_count % 50 == 0:
        _checkpoint()

def log_decision(raw_signal: str, llm_decision: str, llm_reasoning: str, executed: bool = False):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO decisions (timestamp, raw_signal, llm_decision, llm_reasoning, executed) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(WIB).isoformat(), raw_signal, llm_decision, llm_reasoning, int(executed)),
        )

def get_recent_trades(limit: int = 3) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, side, price, qty, amount_idr, order_type, status, pnl, reason, paper_trade FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "side": r[2], "price": r[3],
         "qty": r[4], "amount_idr": r[5], "order_type": r[6],
         "status": r[7], "pnl": r[8], "reason": r[9], "paper_trade": r[10]}
        for r in rows
    ]

def get_trade_count_today() -> int:
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp LIKE ?", (f"{today}%",)
        ).fetchone()
    return row[0] if row else 0

def count_new_completed_sells(since_id: int = 0) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE side='sell' AND pnl IS NOT NULL AND id > ?",
            (since_id,),
        ).fetchone()
    return row[0] if row else 0

def get_max_trade_id() -> int:
    with _conn() as conn:
        row = conn.execute("SELECT MAX(id) FROM trades").fetchone()
    return row[0] if row and row[0] else 0

def get_recent_completed_sells(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE side='sell' AND pnl IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r[0], "timestamp": r[1], "side": r[2], "price": r[3],
            "qty": r[4], "amount_idr": r[5], "order_type": r[6],
            "status": r[7], "pnl": r[8], "reason": r[9], "paper_trade": r[10],
        })
    return result

def get_trades_by_period(period: str = "day") -> list[dict]:
    now = datetime.now(WIB)
    if period == "day":
        prefix = now.strftime("%Y-%m-%d")
        like = f"{prefix}%"
    elif period == "month":
        prefix = now.strftime("%Y-%m")
        like = f"{prefix}%"
    elif period == "year":
        prefix = now.strftime("%Y")
        like = f"{prefix}%"
    else:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp DESC",
            (like,),
        ).fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r[0], "timestamp": r[1], "side": r[2], "price": r[3],
            "qty": r[4], "amount_idr": r[5], "order_type": r[6],
            "status": r[7], "pnl": r[8], "reason": r[9], "paper_trade": r[10],
        })
    return result

def init_chat_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL,
                message TEXT NOT NULL
            )
        """)

def save_chat(role: str, message: str):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO chat_history (timestamp, role, message) VALUES (?, ?, ?)",
            (datetime.now(WIB).isoformat(), role, message),
        )
        conn.execute("DELETE FROM chat_history WHERE id NOT IN (SELECT id FROM chat_history ORDER BY id DESC LIMIT 50)")

def get_chat_history(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, message FROM chat_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    rows.reverse()
    return [{"role": r[0], "message": r[1]} for r in rows]
