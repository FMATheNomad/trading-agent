import time
import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
import httpx
import config
from executor import place_order, get_order, cancel_order, get_balance
from notifier import send_message
from data_layer import fetch_all_tickers
import asyncio

_WIB = timezone(timedelta(hours=7))
_DCA_DATA_DIR = os.getenv("STATE_DIR") or os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(__file__))
_DCA_STATE_FILE = os.path.join(_DCA_DATA_DIR, "dca_state.json")
_DCA_DB_PATH = os.path.join(_DCA_DATA_DIR, "dca_trades.db")

DCA_IDLE = 0
DCA_BASE_PLACED = 1
DCA_FILLED = 2
DCA_TP_PLACED = 3
DCA_CANCELLED = 4

DCA_CONFIGS = {
    "btc_idr": {"safety_steps": [0.05, 0.10, 0.15, 0.20, 0.25], "tp": 0.20, "min_order": 20000},
    "eth_idr": {"safety_steps": [0.05, 0.10, 0.15, 0.20], "tp": 0.20, "min_order": 20000},
    "usdt_idr": {"safety_steps": [0.005, 0.01, 0.015], "tp": 999, "min_order": 20000},
}

_dca_write_count = 0

def _dca_checkpoint():
    try:
        with sqlite3.connect(_DCA_DB_PATH) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

def _dca_db_init():
    os.makedirs(os.path.dirname(_DCA_DB_PATH) if os.path.dirname(_DCA_DB_PATH) else ".", exist_ok=True)
    with sqlite3.connect(_DCA_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dca_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL,
                qty REAL,
                amount_idr REAL,
                order_type TEXT,
                status TEXT DEFAULT 'simulated',
                pnl REAL,
                reason TEXT
            )
        """)

def _dca_log_trade(side: str, price: float, qty: float, amount_idr: float,
                   order_type: str = "limit", status: str = "simulated",
                   pnl: float | None = None, reason: str = ""):
    global _dca_write_count
    try:
        with sqlite3.connect(_DCA_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO dca_trades (timestamp, side, price, qty, amount_idr, order_type, status, pnl, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(_WIB).isoformat(), side, price, qty,
                 amount_idr, order_type, status, pnl, reason),
            )
        _dca_write_count += 1
        if _dca_write_count % 50 == 0:
            _dca_checkpoint()
    except Exception as e:
        print(f"  DCA log error: {e}", flush=True)

class DCAInstance:
    def __init__(self, pair: str):
        self.pair = pair
        self.state = DCA_IDLE
        self.base_entry = 0
        self.base_qty = 0
        self.base_invest = 0
        self.base_order_id = None
        self.safety_orders: list[dict] = []
        self.safety_filled = 0
        self.avg_entry = 0
        self.total_qty = 0
        self.total_invest = 0
        self.tp_order_id = None
        self.tp_price = 0
        self.created_at = time.time()
        self.active = True

class SmartDCA:
    def __init__(self):
        self.instances: dict[str, DCAInstance] = {}
        self.scan_interval = 60
        self.last_scan = 0
        self.ticker_map: dict = {}
        self.balance_idr: float = 0
        self._ticker_stale: bool = False
        self._balance_stale: bool = False
        _dca_db_init()

    async def scan_and_place(self, ticker_map: dict, balance_idr: float,
                              existing_positions: set[str] | None = None,
                              blacklisted: set[str] | None = None):
        now = time.time()
        if now - self.last_scan < self.scan_interval:
            return
        self.last_scan = now

        cfg = DCA_CONFIGS
        for pair, dca in list(self.instances.items()):
            if dca.state in (DCA_TP_PLACED, DCA_CANCELLED):
                self.instances.pop(pair, None)

        for pair, settings in cfg.items():
            if pair in self.instances and self.instances[pair].active:
                continue
            if existing_positions and pair in existing_positions:
                continue
            if blacklisted and pair in blacklisted:
                continue

            price = float(ticker_map.get(pair, {}).get("sell", 0))
            if price < 50:
                continue
            settings = cfg[pair]
            invest = settings["min_order"]
            if invest > balance_idr * 0.5:
                continue
            invest = min(invest, int(balance_idr * 0.5))

            try:
                async with httpx.AsyncClient() as c:
                    order = await place_order(c, "buy", price, invest, pair=pair, order_type="market")
                    if order.get("order_id") or order.get("receive_rp"):
                        coin = pair.split("_")[0]
                        fill_qty = float(order.get(f"receive_{coin}", 0)) or (invest / price)
                        fill_price = invest / fill_qty if fill_qty > 0 else price
                        di = DCAInstance(pair)
                        di.state = DCA_BASE_PLACED
                        di.base_entry = fill_price
                        di.base_qty = fill_qty
                        di.base_invest = invest
                        di.avg_entry = fill_price
                        di.total_qty = fill_qty
                        di.total_invest = invest
                        di.base_order_id = order.get("order_id")
                        self.instances[pair] = di
                        _dca_log_trade("buy", fill_price, fill_qty, invest, order_type="maker_first", status="filled", reason=f"dca_base {pair}")
                        print(f"  DCA BASE: {pair} @ Rp{fill_price:,.0f} qty={fill_qty:.6f}", flush=True)
                        await send_message(f"🏦 DCA BASE: {pair}\nRp{fill_price:,.0f} × {fill_qty:.4f}")
                        self._place_safety_limits(c, di, settings, fill_price, invest)
                        for so in di.safety_orders:
                            await self._place_safety_order(c, di, so)
            except Exception as e:
                print(f"  DCA base failed {pair}: {e}", flush=True)

    def _place_safety_limits(self, client, di, settings, entry, invest):
        for i, step in enumerate(settings["safety_steps"]):
            limit_price = int(entry * (1 - step))
            so = {"price": limit_price, "invest": invest, "qty": invest / limit_price, "order_id": None, "filled": False, "step": step}
            di.safety_orders.append(so)

    async def _place_safety_order(self, client, di, so):
        order = await place_order(client, "buy", so["price"], so["invest"], pair=di.pair, order_type="limit")
        if order.get("order_id"):
            so["order_id"] = int(order["order_id"])
            print(f"  DCA SAFETY LIMIT: {di.pair} @ Rp{so['price']:,} step -{so['step']*100:.0f}%", flush=True)

    async def check_fills(self, client: httpx.AsyncClient, ticker_map: dict):
        for pair, di in list(self.instances.items()):
            if not di.active:
                continue
            for so in di.safety_orders:
                if so["filled"] or not so["order_id"]:
                    continue
                try:
                    oi = await get_order(client, so["order_id"], pair=pair)
                    if oi:
                        status = oi.get("status", "").lower()
                        if status in ("filled",) or float(oi.get(f"remain_{pair.split('_')[0]}", 1)) <= 0:
                            so["filled"] = True
                            di.safety_filled += 1
                            fill_price = float(oi.get("price", so["price"]))
                            fill_qty = float(oi.get(f"receive_{pair.split('_')[0]}", 0))
                            if fill_qty <= 0:
                                fill_qty = so["invest"] / fill_price
                            total_invest = di.total_invest + so["invest"]
                            total_qty = di.total_qty + fill_qty
                            di.avg_entry = total_invest / total_qty if total_qty > 0 else di.avg_entry
                            di.total_invest = total_invest
                            di.total_qty = total_qty
                            _dca_log_trade("buy", fill_price, fill_qty, so["invest"], order_type="limit", status="filled", reason=f"dca_safety {pair} -{so['step']*100:.0f}%")
                            print(f"  DCA SAFETY: {pair} @ Rp{fill_price:,.0f} step -{so['step']*100:.0f}%", flush=True)
                            await send_message(f"🏦 DCA SAFETY: {pair}\n-{so['step']*100:.0f}% @ Rp{fill_price:,.0f}")
                        elif status in ("cancelled", "rejected"):
                            so["order_id"] = None
                except Exception:
                    pass

    async def place_safety_and_tp(self, client: httpx.AsyncClient):
        cfg = DCA_CONFIGS
        for pair, di in list(self.instances.items()):
            if not di.active:
                continue
            settings = cfg.get(pair)
            if not settings:
                continue
            entry = di.base_entry
            for i, so in enumerate(di.safety_orders):
                if so["filled"] or so["order_id"] is not None:
                    continue
                await self._place_safety_order(client, di, so)

            if di.state == DCA_TP_PLACED and di.tp_order_id:
                try:
                    oi = await get_order(client, di.tp_order_id, pair=pair)
                    if oi:
                        status = oi.get("status", "").lower()
                        if status in ("filled",) or float(oi.get(f"remain_{pair.split('_')[0]}", 1)) <= 0:
                            fill_price = float(oi.get("price", di.tp_price))
                            pnl = (fill_price - di.avg_entry) * di.total_qty
                            _dca_log_trade("sell", fill_price, di.total_qty, di.total_invest, order_type="limit", status="closed", pnl=pnl, reason=f"dca_tp {pair}")
                            print(f"  DCA TP FILLED: {pair} profit Rp{pnl:+,.0f}", flush=True)
                            await send_message(f"✅ DCA TP: {pair}\nProfit Rp{pnl:+,.0f}")
                            self.instances.pop(pair, None)
                except Exception:
                    pass
                continue

            if di.total_qty > 0 and di.safety_filled >= min(1, len(di.safety_orders)):
                tp_price = int(di.avg_entry * (1 + settings["tp"]))
                try:
                    order = await place_order(client, "sell", tp_price, di.total_qty * tp_price, pair=pair, order_type="limit", qty=di.total_qty)
                    if order.get("order_id"):
                        di.tp_order_id = int(order["order_id"])
                        di.tp_price = tp_price
                        di.state = DCA_TP_PLACED
                        print(f"  DCA TP: {pair} SELL limit @ Rp{tp_price:,} (+{settings['tp']*100:.0f}%)", flush=True)
                except Exception as e:
                    print(f"  DCA TP failed {pair}: {e}", flush=True)

    def save_instances(self):
        try:
            os.makedirs(os.path.dirname(_DCA_STATE_FILE) if os.path.dirname(_DCA_STATE_FILE) else ".", exist_ok=True)
            data = []
            for pair, di in self.instances.items():
                if not di.active:
                    continue
                safety_data = []
                for so in di.safety_orders:
                    safety_data.append({
                        "price": so["price"], "invest": so["invest"],
                        "qty": so["qty"], "order_id": so["order_id"],
                        "filled": so["filled"], "step": so["step"],
                    })
                data.append({
                    "pair": pair, "state": di.state,
                    "base_entry": di.base_entry, "base_qty": di.base_qty,
                    "base_invest": di.base_invest, "avg_entry": di.avg_entry,
                    "total_qty": di.total_qty, "total_invest": di.total_invest,
                    "created_at": di.created_at,
                    "safety_filled": di.safety_filled,
                    "safety_orders": safety_data,
                    "tp_order_id": di.tp_order_id, "tp_price": di.tp_price,
                })
            tmp = _DCA_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _DCA_STATE_FILE)
        except Exception:
            pass

    def load_instances(self):
        try:
            if not os.path.exists(_DCA_STATE_FILE):
                return
            with open(_DCA_STATE_FILE) as f:
                data = json.load(f)
            if not data:
                return
            for d in data:
                di = DCAInstance(d["pair"])
                di.state = d.get("state", DCA_IDLE)
                di.base_entry = d.get("base_entry", 0)
                di.base_qty = d.get("base_qty", 0)
                di.base_invest = d.get("base_invest", 0)
                di.avg_entry = d.get("avg_entry", 0)
                di.total_qty = d.get("total_qty", 0)
                di.total_invest = d.get("total_invest", 0)
                di.created_at = d.get("created_at", time.time())
                di.safety_filled = d.get("safety_filled", 0)
                di.safety_orders = []
                for so in d.get("safety_orders", []):
                    di.safety_orders.append({
                        "price": so["price"], "invest": so["invest"],
                        "qty": so["qty"], "order_id": so.get("order_id"),
                        "filled": so.get("filled", False), "step": so["step"],
                    })
                di.tp_order_id = d.get("tp_order_id")
                di.tp_price = d.get("tp_price", 0)
                di.state = DCA_TP_PLACED if di.tp_order_id else DCA_BASE_PLACED
                di.active = True
                self.instances[di.pair] = di
        except Exception:
            pass

    async def _fetch_tickers(self, client):
        try:
            self.ticker_map = await fetch_all_tickers(client)
            self._ticker_stale = False
            return True
        except Exception:
            self._ticker_stale = True
            return False

    async def _fetch_balance(self, client):
        try:
            info = await get_balance(client)
            self.balance_idr = float(info.get("balance", {}).get("idr", 0))
            self._balance_stale = False
            return True
        except Exception:
            self._balance_stale = True
            return False

    async def run_cycle(self, client):
        ticker_ok = await self._fetch_tickers(client)
        bal_ok = await self._fetch_balance(client)
        if not ticker_ok or not bal_ok:
            stale_since = "ticker" if not ticker_ok else "balance"
            print(f"  DCA SKIP CYCLE: stale data ({stale_since})", flush=True)
            return
        if not self.ticker_map:
            print(f"  DCA SKIP CYCLE: ticker_map kosong", flush=True)
            return
        if self.balance_idr < 10000:
            print(f"  DCA SKIP CYCLE: balance Rp{self.balance_idr:,.0f} < 10k", flush=True)
            return
        await self.check_fills(client, self.ticker_map)
        await self.place_safety_and_tp(client)
        await self.scan_and_place(self.ticker_map, self.balance_idr)
        self.save_instances()
