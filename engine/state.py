# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

"""engine/state.py — pemilik tunggal state runtime bot.

Sebelumnya 50 nama ini hidup sebagai global di `main.py` dan setiap fungsi
menempelkannya lewat `global X`, jadi tidak ada batas siapa yang boleh menulis
`positions`, `_position_states`, atau streak circuit breaker. Sekarang state
tinggal di satu modul dan konsumen wajib lewat `S.<nama>` — mutasi terlihat
eksplisit di call site, dan modul engine lain (cycle, position_sm) bisa memakainya
tanpa mengimpor `main.py` (yang akan jadi circular).
"""

import asyncio

import config
from dca_smart import SmartDCA
from grid_mini import GridMini
from hmm_regime import HMMRegimeDetector
from momentum import MomentumEngine
from optimizer import AIOptimizer
from orders import registry
from risk_manager import PortfolioRiskManager, RiskManager
from state_bus import StateBus


risk = RiskManager()
portfolio_risk = PortfolioRiskManager()
hmm_detector = HMMRegimeDetector(n_states=config.HMM_N_STATES)
positions: list[dict] = []
shutdown_flag = False
momentum_engine = MomentumEngine()
optimizer = AIOptimizer()
grid_mini = GridMini()
dca_smart = SmartDCA()

regime_history: list[str] = []
known_pairs: set[str] = set()
_ext_entry_prices: dict[str, float] = {}
_pair_meta: dict[str, dict] = {}
_hmm_trained_cycle = 0
_prev_regime: str = ""
_prev_equity: float = 0
_report_sent_count: int = 0
_coin_blacklist: set[str] = set()
_cio_stats: dict = {"total_decisions": 0, "buys": 0, "sells": 0, "wins": 0, "losses": 0}
_cooldown: dict[str, float] = {}
_latest_regime: dict = {}
_latest_ticker_map: dict = {}
_latest_all_signals: dict = {}
_latest_ohlcv_map_1h: dict = {}
_order_error_cooldown: dict[str, float] = {}
_realtime_sltp_last: dict[str, float] = {}
_realtime_sold: set[str] = set()
_realtime_sold_time: dict[str, float] = {}
_position_states: dict[str, dict] = {}
_sm_cooldown: dict[str, float] = {}
_momentum_entry_time: dict[str, float] = {}
_pyramid_cooldown: dict[str, float] = {}
_cycle_last_end: float = 0
_cycle_last_info: dict = {}
_recent_actions: list[dict] = []
_realized_pnl_idr: float = 0.0
_rothschild_active: bool = False
_regime_bull_streak: int = 0
_regime_bear_streak: int = 0
_daily_loss_hit_today: bool = False
_greed_used_today: bool = False
_cb_consecutive_loss_days: int = 0
_cb_last_loss_date: str = ""
_cb_triggered_at: float = 0
_cb_active_until: float = 0
_paper_balance: float = 0
_latest_balance: float = 0
cycle_counter = 0

state_bus = StateBus()
_state_lock = asyncio.Lock()


def _engine_order_ids() -> set[str]:
    """Order resting milik state machine engine — dibaca live, tidak disalin.

    Grid/DCA/position-SM punya field order id sendiri; tanpa ini orphan-cleanup
    melihat order mereka sebagai order yatim lalu membatalkannya.
    """
    ids: set[str] = set()
    for sm in _position_states.values():
        ids |= {x for x in (sm.get("tp_order_id"), sm.get("sl_order_id")) if x}
    for gi in grid_mini.instances:
        ids |= {x for x in (gi.buy_order_id, gi.sell_order_id) if x}
    for di in dca_smart.instances.values():
        ids |= {x for x in (di.base_order_id, di.tp_order_id) if x}
        ids |= {x for x in (so.get("order_id") for so in di.safety_orders) if x}
    return {str(x) for x in ids}


registry.add_engine_source(_engine_order_ids)
