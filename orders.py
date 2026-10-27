# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""orders.py — satu registry in-memory untuk order yang sedang bot lacak.

Sebelumnya order yang menunggu fill tersebar di beberapa dict global dengan
bentuk record berbeda (`_pending_orders`, `_pending_sells`, `_tp_limit_orders`),
sehingga tidak ada satu tempat yang bisa menjawab "order apa yang masih kita
track di pair ini?" — dan orphan-cleanup tidak tahu bahwa order itu milik
grid/DCA, lalu membatalkannya sebagai order yatim.

Satu order aktif per (pair, kind): cocok dengan perilaku lama (dulu key dict =
pair). Engine yang menaruh order resting (grid/DCA/position-SM) tetap menyimpan
order id di state machine masing-masing, tapi mendaftarkan dirinya sebagai
"engine source" supaya registry tetap bisa jadi satu-satunya tempat bertanya
apakah sebuah order id milik bot.
"""

from dataclasses import dataclass

ENTRY = "entry"        # buy maker yang menunggu fill (rules engine)
SELL = "sell"          # sell limit (TP/SL/auto-sell) yang menunggu fill
GRID_BUY = "grid_buy"
GRID_SELL = "grid_sell"
DCA_BUY = "dca_buy"
DCA_TP = "dca_tp"

ALL_KINDS = (ENTRY, SELL, GRID_BUY, GRID_SELL, DCA_BUY, DCA_TP)
ENGINE_KINDS = (GRID_BUY, GRID_SELL, DCA_BUY, DCA_TP)


@dataclass
class Tracked:
    pair: str
    kind: str
    order_id: str
    side: str = "buy"
    qty: float = 0.0
    price: float = 0.0
    amount_idr: float = 0.0
    is_maker: bool = False
    atr_pct: float | None = None
    client_order_id: str = ""
    cycles: int = 0


class OrderRegistry:
    def __init__(self) -> None:
        self._orders: dict[tuple[str, str], Tracked] = {}
        self._engine_sources: list = []

    def add_engine_source(self, fn) -> None:
        """Daftarkan aksesible mesin (grid/DCA/position-SM) yang menyimpan order
        resting di state machine-nya sendiri. Registry jadi bisa menjawab
        "order ini punya kita?" tanpa merampas state mereka.

        fn() wajib mengembalikan iterable order id (bebas, boleh None campur).
        """
        self._engine_sources.append(fn)

    def engine_ids(self) -> set[str]:
        ids: set[str] = set()
        for fn in self._engine_sources:
            for oid in fn():
                if oid is not None:
                    ids.add(str(oid))
        return ids

    def owns(self, order_id) -> bool:
        want = str(order_id)
        if want in {t.order_id for t in self._orders.values()}:
            return True
        return want in self.engine_ids()

    def add(self, pair: str, kind: str, order_id, **fields) -> Tracked:
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown order kind {kind!r}")
        t = Tracked(pair=pair, kind=kind, order_id=str(order_id), **fields)
        self._orders[(pair, kind)] = t
        return t

    def get(self, pair: str, kind: str) -> Tracked | None:
        return self._orders.get((pair, kind))

    def has(self, pair: str, kind: str | None = None) -> bool:
        if kind is not None:
            return (pair, kind) in self._orders
        return any(p == pair for (p, _) in self._orders)

    def find(self, order_id) -> Tracked | None:
        want = str(order_id)
        for t in self._orders.values():
            if t.order_id == want:
                return t
        return None

    def pop(self, pair: str, kind: str) -> Tracked | None:
        return self._orders.pop((pair, kind), None)
    def drop_pair(self, pair: str, kinds: tuple[str, ...] = ALL_KINDS) -> list[Tracked]:
        dropped = []
        for (p, k), t in list(self._orders.items()):
            if p == pair and k in kinds:
                dropped.append(self._orders.pop((p, k)))
        return dropped

    def bump(self, pair: str, kind: str) -> int:
        """Naikkan counter siklus grace; 0 kalau order sudah tidak dilacak."""
        t = self._orders.get((pair, kind))
        if not t:
            return 0
        t.cycles += 1
        return t.cycles

    def list(self, kind: str | None = None) -> list[Tracked]:
        """Snapshot list — aman diiterasi sambil pop/drop."""
        return [t for t in self._orders.values() if kind is None or t.kind == kind]

    def tracked_pairs(self, kinds: tuple[str, ...] | None = None) -> set[str]:
        return {p for (p, k) in self._orders if kinds is None or k in kinds}

    def reset(self) -> None:
        self._orders.clear()

    def __len__(self) -> int:
        return len(self._orders)


registry = OrderRegistry()
