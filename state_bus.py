# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""state_bus.py — jalur satu arah dari task websocket ke pemegang state.

Task WS (market_ws / private_ws) dulu men-spawn coroutine tak terbatas
(`asyncio.ensure_future`) yang langsung mutasi `positions`, `_position_states`
dan menaruh/batal order di exchange. Akibatnya dua tick bisa masuk ke cabang
state machine yang sama bergantian — dan bersaingan dengan portfolio cycle.

Aturannya sekarang: WS cuma `publish()` (sinkron, zero await, tidak boleh
menyentuh state apa pun), dan satu consumer satu waktu memegang
`asyncio.Lock` yang sama dengan cycle untuk memproses antrean.

Tick di-coalesce per pair (yang penting harga terakhir), fill disimpan
terbatas supaya burst tidak menghabiskan memori.
"""

from dataclasses import dataclass
import asyncio

TICK = "tick"
FILL = "fill"

MAX_PENDING_FILLS = 256


@dataclass
class Event:
    kind: str
    pair: str
    price: float = 0.0
    side: str = ""
    status: str = ""


class StateBus:
    def __init__(self) -> None:
        self._ticks: dict[str, float] = {}
        self._fills: list[Event] = []
        self._wake = None
        self.dropped_fills = 0

    def wake_event(self):
        """Event loop dibuat malas (saat loop sudah ada), bukan saat import."""
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def publish_tick(self, pair: str, price: float) -> None:
        self._ticks[pair] = price
        self.wake_event().set()

    def publish_fill(self, pair: str, side: str, status: str) -> bool:
        if len(self._fills) >= MAX_PENDING_FILLS:
            self.dropped_fills += 1
            return False
        self._fills.append(Event(kind=FILL, pair=pair, side=side, status=status))
        self.wake_event().set()
        return True

    def drain(self) -> list[Event]:
        """Ambil seluruh antrean sekaligus. Sinkron: tidak ada publish yang
        bisa nyelip di tengah snapshot-then-clear."""
        events = [Event(kind=TICK, pair=p, price=x) for p, x in self._ticks.items()]
        events += self._fills
        self._ticks.clear()
        self._fills.clear()
        ev = self._wake
        if ev is not None:
            ev.clear()
        return events

    def pending(self) -> int:
        return len(self._ticks) + len(self._fills)

    async def wait(self) -> None:
        await self.wake_event().wait()
