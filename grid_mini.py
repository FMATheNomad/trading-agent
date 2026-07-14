import time
import math
import httpx
import config
from executor import place_order
from db import log_trade
import asyncio

GRID_STATE_IDLE = 0
GRID_STATE_BUY_PLACED = 1
GRID_STATE_FILLED = 2
GRID_STATE_SELL_PLACED = 3

class GridInstance:
    def __init__(self, pair: str, entry_price: float, investment: int):
        self.pair = pair
        self.entry_price = entry_price
        self.investment = investment
        self.qty = 0
        self.state = GRID_STATE_BUY_PLACED
        self.buy_order_id = None
        self.sell_order_id = None
        self.created_at = time.time()
        self.grid_level = 0
        self.tp_price = 0

class GridMini:
    def __init__(self):
        self.instances: list[GridInstance] = []
        self.max_instances = 2
        self.min_volume_idr = 500_000_000
        self.max_grid_levels = 3
        self.grid_step_pct = 0.01
        self.tp_pct = 0.01
        self.last_scan = 0
        self.scan_interval = 30
        self._scanning_pairs: list[str] = []
        self._pair_blacklist: set[str] = set()
        self._warned_maker_first = False

    async def scan_and_place(self, ticker_map: dict, ohlcv_map: dict, regime: str, balance_idr: float):
        now = time.time()
        if now - self.last_scan < self.scan_interval:
            return
        self.last_scan = now

        if regime not in ("SIDEWAYS", "SIDEWAYS_LOW_VOL", "BULL"):
            return

        active_pairs = {g.pair for g in self.instances}
        slots = self.max_instances - len(self.instances)
        if slots <= 0:
            return

        candidates = []
        for pair, t in ticker_map.items():
            if pair in active_pairs or pair in self._pair_blacklist:
                continue
            if pair in config.STABLECOINS or pair in config.SKIP_COINS:
                continue
            vol = float(t.get("vol_idr", 0))
            if vol < self.min_volume_idr:
                continue
            sell_price = float(t.get("sell", 0))
            if sell_price < 50:
                continue
            ohlcv = ohlcv_map.get(pair, [])
            if len(ohlcv) < 20:
                continue
            closes = [float(c["close"]) for c in ohlcv[-20:]]
            atr_pct = ((max(closes) - min(closes)) / min(closes)) * 100 if min(closes) > 0 else 0
            if atr_pct < 1.0 or atr_pct > 10.0:
                continue
            range_14 = ohlcv[-14:]
            highs = [float(c["high"]) for c in range_14]
            lows = [float(c["low"]) for c in range_14]
            r_high = max(highs)
            r_low = min(lows)
            r_range = r_high - r_low
            pp = ((sell_price - r_low) / r_range * 100) if r_range > 0 else 50
            if pp < 20 or pp > 80:
                continue
            score = 0
            if atr_pct < 3: score += 1
            if vol > 1_000_000_000: score += 1
            if 30 <= pp <= 70: score += 1
            candidates.append((pair, sell_price, atr_pct, vol, score))

        candidates.sort(key=lambda x: -x[4])
        for pair, price, atr, vol, sc in candidates[:slots]:
            investment = min(balance_idr * 0.4, 50000)
            if investment < config.MIN_ORDER_IDR:
                continue
            investment = max(investment, config.MIN_ORDER_IDR)
            grid_entry = price * (1 - self.grid_step_pct)
            qty = investment / grid_entry
            if qty <= 0:
                continue
            try:
                async with httpx.AsyncClient() as c:
                    order = await place_order(c, "buy", grid_entry, investment, pair=pair, order_type="limit")
                    if order.get("order_id"):
                        oid = int(order["order_id"])
                        gi = GridInstance(pair, grid_entry, investment)
                        gi.buy_order_id = oid
                        gi.state = GRID_STATE_BUY_PLACED
                        self.instances.append(gi)
                        print(f"  GRID MINI: BUY limit {pair} @ Rp{grid_entry:,.0f} (Rp{investment:,}) oid={oid}", flush=True)
                        balance_idr -= investment
                    elif order.get("paper_trade"):
                        gi = GridInstance(pair, grid_entry, investment)
                        gi.state = GRID_STATE_FILLED
                        gi.qty = qty
                        gi.entry_price = grid_entry
                        self.instances.append(gi)
                        print(f"  GRID MINI [PAPER]: BUY {pair} @ Rp{grid_entry:,.0f} (Rp{investment:,})", flush=True)
                        balance_idr -= investment
            except Exception as e:
                print(f"  GRID MINI order failed {pair}: {e}", flush=True)

    async def check_fills_and_place_tp(self, client: httpx.AsyncClient, ticker_map: dict):
        for gi in list(self.instances):
            if gi.state == GRID_STATE_BUY_PLACED and gi.buy_order_id:
                try:
                    from executor import get_order
                    oi = await get_order(client, gi.buy_order_id, pair=gi.pair)
                    if oi:
                        status = oi.get("status", "").lower()
                        if status in ("filled",) or float(oi.get(f"remain_{gi.pair.split('_')[0]}", 1)) <= 0:
                            gi.state = GRID_STATE_FILLED
                            fill_price = float(oi.get("price", gi.entry_price))
                            fill_qty = float(oi.get(f"receive_{gi.pair.split('_')[0]}", 0))
                            if fill_qty <= 0:
                                fill_qty = gi.investment / fill_price
                            gi.qty = fill_qty
                            gi.entry_price = fill_price
                            gi.tp_price = int(fill_price * (1 + self.tp_pct))
                            print(f"  GRID MINI FILLED: {gi.pair} @ Rp{fill_price:,.0f} qty={fill_qty:.6f}", flush=True)
                            log_trade("buy", fill_price, fill_qty, gi.investment, order_type="limit", status="filled", reason=f"grid_mini_entry")
                        elif status in ("cancelled", "rejected"):
                            print(f"  GRID MINI CANCELLED: {gi.pair} oid={gi.buy_order_id}", flush=True)
                            self._pair_blacklist.add(gi.pair)
                            self.instances.remove(gi)
                except Exception:
                    pass

            if gi.state == GRID_STATE_FILLED and gi.qty > 0:
                tp_price = gi.tp_price
                if tp_price <= 0:
                    tp_price = int(gi.entry_price * (1 + self.tp_pct))
                try:
                    bid = int(ticker_map.get(gi.pair, {}).get("buy", 0))
                    if bid > 0 and bid >= gi.entry_price:
                        tp_price = max(tp_price, bid)
                    async with httpx.AsyncClient() as c:
                        from executor import place_order
                        order = await place_order(c, "sell", tp_price, gi.qty * tp_price, pair=gi.pair, order_type="limit", qty=gi.qty)
                        if order.get("order_id"):
                            gi.sell_order_id = int(order["order_id"])
                            gi.state = GRID_STATE_SELL_PLACED
                            print(f"  GRID MINI TP: {gi.pair} SELL limit @ Rp{tp_price:,.0f} (target +{self.tp_pct*100:.0f}%)", flush=True)
                        elif order.get("paper_trade"):
                            pnl = (tp_price - gi.entry_price) * gi.qty
                            log_trade("sell", tp_price, gi.qty, gi.qty * tp_price, order_type="limit", status="closed", pnl=pnl, reason=f"grid_mini_tp {gi.pair}")
                            print(f"  GRID MINI [PAPER] TP: {gi.pair} profit Rp{pnl:+,.0f}", flush=True)
                            self.instances.remove(gi)
                except Exception as e:
                    print(f"  GRID MINI TP failed {gi.pair}: {e}", flush=True)

    async def check_sell_fills(self, client: httpx.AsyncClient):
        for gi in list(self.instances):
            if gi.state != GRID_STATE_SELL_PLACED or not gi.sell_order_id:
                continue
            try:
                from executor import get_order
                oi = await get_order(client, gi.sell_order_id, pair=gi.pair)
                if oi:
                    status = oi.get("status", "").lower()
                    if status in ("filled",) or float(oi.get(f"remain_{gi.pair.split('_')[0]}", 1)) <= 0:
                        fill_p = float(oi.get("price", gi.tp_price or 0))
                        pnl = (fill_p - gi.entry_price) * gi.qty
                        log_trade("sell", fill_p, gi.qty, gi.qty * fill_p, order_type="limit", status="closed", pnl=pnl, reason=f"grid_mini_tp {gi.pair}")
                        print(f"  GRID MINI TP FILLED: {gi.pair} profit Rp{pnl:+,.0f}", flush=True)
                        self.instances.remove(gi)
                    elif status in ("cancelled", "rejected"):
                        print(f"  GRID MINI TP CANCELLED: {gi.pair} oid={gi.sell_order_id}", flush=True)
                        gi.state = GRID_STATE_FILLED
                        gi.sell_order_id = None
            except Exception:
                pass

    def cleanup_stale(self):
        now = time.time()
        for gi in list(self.instances):
            if now - gi.created_at > 86400:
                print(f"  GRID MINI CLEANUP: {gi.pair} stale (>24h), remove", flush=True)
                self.instances.remove(gi)
        if len(self._pair_blacklist) > 50:
            self._pair_blacklist.clear()
