# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import numpy as np
import pandas as pd
import config

class RiskManager:
    def __init__(self):
        self.daily_start_balance = config.PLAY_CAPITAL_IDR
        self.today_peak = 0
        self.trailing_highs: dict[str, float] = {}
        self._initial_sl_released: dict[str, bool] = {}
        self._pyramid_done: dict[str, bool] = {}
        self.daily_target_hit = False
        self.daily_loss_stopped = False

    def check_daily_limits(self, total_equity: float) -> str | None:
        if total_equity > self.today_peak:
            self.today_peak = total_equity
        daily_loss = self.today_peak - total_equity
        if daily_loss >= config.DAILY_LOSS_FLOOR_IDR and not self.daily_loss_stopped:
            self.daily_loss_stopped = True
            return "DAILY_LOSS_LIMIT"
        if total_equity > self.today_peak - config.MIN_ORDER_IDR and self.daily_loss_stopped:
            self.daily_loss_stopped = False
            self.today_peak = total_equity
        return None

    def should_stop_trading(self, total_equity: float) -> bool:
        if total_equity > self.today_peak:
            self.today_peak = total_equity
        if total_equity < config.DAILY_LOSS_FLOOR_IDR:
            return True
        return False

    def compute_position_size(self, balance_idr: float) -> float:
        raw = balance_idr * config.POSITION_SIZE_PCT
        return max(raw, config.MIN_ORDER_IDR)

    def estimate_fee(self, amount_idr: float) -> float:
        return amount_idr * config.TAKER_FEE_PCT

    def is_profit_viable(self, entry_price: float, qty: float, side: str, atr_pct: float | None = None,
                         is_tp_maker: bool = True) -> bool:
        atr = max(atr_pct or 1.5, 1.5)
        target_mult = atr * config.ATR_TP_MULTIPLIER / 100
        if side.upper() == "BUY":
            target = entry_price * (1 + target_mult)
        else:
            target = entry_price * (1 - target_mult)
        gross = abs(target - entry_price) * qty
        entry_fee = config.MAKER_FEE_PCT if config.MAKER_FIRST else config.TAKER_FEE_PCT
        exit_fee = (config.MAKER_FEE_PCT + config.SELL_TAX_PCT) if is_tp_maker else (config.TAKER_FEE_PCT + config.SELL_TAX_PCT)
        fee_roundtrip = (entry_price * qty * entry_fee) + (target * qty * exit_fee)
        if gross <= fee_roundtrip:
            return False
        sl_mult = atr * config.ATR_SL_MULTIPLIER / 100
        loss_if_sl = abs(entry_price * sl_mult) * qty + fee_roundtrip
        rr_ratio = gross / max(loss_if_sl, 1e-6)
        if rr_ratio < 0.8:
            return False
        return True

    def get_sl_tp(self, entry_price: float, side: str, atr_pct: float | None = None) -> tuple[float, float]:
        atr = max(atr_pct or 1.5, 0.5)
        sl_mult = atr * config.ATR_SL_MULTIPLIER
        tp_mult = atr * config.ATR_TP_MULTIPLIER
        if side.upper() == "BUY":
            sl = entry_price * (1 - sl_mult / 100)
            tp = entry_price * (1 + tp_mult / 100)
        else:
            sl = entry_price * (1 + sl_mult / 100)
            tp = entry_price * (1 - tp_mult / 100)
        return round(sl, 2), round(tp, 2)

    def compute_atr(self, ohlcv: list[dict], period: int = 14, clamped: bool = True) -> float:
        if len(ohlcv) < period + 1:
            return config.ATR_SL_MULTIPLIER
        df = pd.DataFrame(ohlcv[-period - 1:])
        df.columns = [c.lower() for c in df.columns]
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.mean()
        atr_pct = round(atr / close.iloc[-1] * 100, 2) if close.iloc[-1] else 1
        if clamped:
            atr_pct = max(atr_pct, 0.5)
        return atr_pct

    def check_sl_tp(self, entry_price: float, current_price: float, side: str, pair: str = "", atr_pct: float | None = None, entry_mode: str = "KONSERVATIF") -> str | None:
        if entry_price <= 0:
            return None
        atr = atr_pct or config.ATR_SL_MULTIPLIER
        if side.upper() == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        if entry_mode == "ROTHSCHILD":
            sl_mult = config.ROTHSCHILD_INITIAL_SL_ATR
            trail_mult = config.ROTHSCHILD_TRAILING_SL_ATR
            pyr_mult = config.ROTHSCHILD_PYRAMID_TRIGGER
            released = self._initial_sl_released.get(pair, False)
            pyr_ok = self._pyramid_done.get(pair, False)

            if not released:
                sl_pct = atr * sl_mult / 100
                if (side.upper() == "BUY" and current_price <= entry_price * (1 - sl_pct)) or \
                   (side.upper() == "SELL" and current_price >= entry_price * (1 + sl_pct)):
                    return "INITIAL_SL"

            pyr_pct = atr * pyr_mult / 100
            if not pyr_ok and pnl_pct >= pyr_pct:
                self._pyramid_done[pair] = True
                return "PYRAMID_TRIGGER"

            if not released and pnl_pct > 0:
                self._initial_sl_released[pair] = True
                released = True
                self.trailing_highs[pair] = current_price

            if released:
                if side.upper() == "BUY" and current_price > self.trailing_highs.get(pair, entry_price):
                    self.trailing_highs[pair] = current_price
                if side.upper() == "SELL" and current_price < self.trailing_highs.get(pair, entry_price):
                    self.trailing_highs[pair] = current_price
                trail_pct = atr * trail_mult / 100
                if side.upper() == "BUY":
                    trail_stop = self.trailing_highs[pair] * (1 - trail_pct)
                    if current_price <= trail_stop:
                        return "TRAILING_SL"
                else:
                    trail_stop = self.trailing_highs[pair] * (1 + trail_pct)
                    if current_price >= trail_stop:
                        return "TRAILING_SL"
        else:
            trail_mult = max(atr * config.ATR_SL_MULTIPLIER * 0.4, 0.5)
            trail_pct = trail_mult / 100
            tp_pct = atr * config.ATR_TP_MULTIPLIER / 100
            trail_activate_pct = max(atr * 0.15, 0.3) / 100
            if side.upper() == "BUY":
                if pair and current_price > self.trailing_highs.get(pair, entry_price):
                    self.trailing_highs[pair] = current_price
                if pair and pnl_pct > trail_activate_pct:
                    trail_stop = self.trailing_highs[pair] * (1 - trail_pct)
                    if current_price <= trail_stop:
                        return "TRAILING_SL"
                if pnl_pct >= tp_pct:
                    return "TP_HIT"
            else:
                if pair and current_price < self.trailing_highs.get(pair, entry_price):
                    self.trailing_highs[pair] = current_price
                if pair and pnl_pct > trail_activate_pct:
                    trail_stop = self.trailing_highs[pair] * (1 + trail_pct)
                    if current_price >= trail_stop:
                        return "TRAILING_SL"
                if pnl_pct >= tp_pct:
                    return "TP_HIT"
        return None


class KellyCalculator:
    def __init__(self):
        self.win_rate = 0.5
        self.avg_win = 0
        self.avg_loss = 0
        self.trade_count = 0

    def update(self, trades_history: list[dict]):
        wins = [t for t in trades_history if t.get("pnl", 0) > 0]
        losses = [t for t in trades_history if t.get("pnl", 0) <= 0]
        self.trade_count = len(wins) + len(losses)
        if self.trade_count < 5:
            return
        self.win_rate = len(wins) / self.trade_count
        self.avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        self.avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 1

    def optimal_fraction(self) -> float:
        if self.trade_count < 5 or self.avg_loss == 0:
            return config.KELLY_FRACTION
        b = self.avg_win / self.avg_loss
        p = self.win_rate
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        half_kelly = max(min(kelly * 0.5, config.MAX_KELLY_ALLOC), config.MIN_KELLY_ALLOC)
        return half_kelly

    def compute_allocation(self, score: int, conviction: str) -> float:
        base = self.optimal_fraction()
        score_boost = min(abs(score) * 0.05, 0.1)
        conv_boost = 0.05 if conviction == "HIGH" else 0
        alloc = base + score_boost + conv_boost
        return round(max(min(alloc, config.MAX_KELLY_ALLOC), config.MIN_KELLY_ALLOC), 2)

PER_REGIME_KELLY = {
    "BULL": 0.25,
    "BEAR": 0.15,
    "SIDEWAYS": 0.10,
    "SIDEWAYS_LOW_VOL": 0.05,
    "HIGH_VOL": 0.05,
}

class PortfolioRiskManager:
    def __init__(self, initial_capital: float = config.PLAY_CAPITAL_IDR):
        self.peak_capital = initial_capital
        self.initial_capital = initial_capital
        self.kelly = KellyCalculator()

    def set_trade_history(self, trades: list[dict]):
        self.kelly.update(trades)

    def kelly_for_regime(self, regime: str) -> float:
        base = PER_REGIME_KELLY.get(regime, 0.10)
        scale = config.KELLY_FRACTION / 0.10
        return min(base * scale, config.MAX_KELLY_ALLOC)

    def check_portfolio_stop(self, total_equity: float) -> bool:
        if total_equity > self.peak_capital:
            self.peak_capital = total_equity
        drawdown = (self.peak_capital - total_equity) / self.peak_capital
        if drawdown >= abs(config.PORTFOLIO_STOP_LOSS_PCT):
            return True
        return False

    def validate_allocation(self, trades: list[dict], current_positions: list[dict],
                             balance_idr: float) -> list[dict]:
        held = {p["pair"]: p.get("current_value", 0) or (p["qty"] * p.get("entry_price", 0)) for p in current_positions if not p.get("real")}
        valid = []
        for t in trades:
            if t.get("action") == "SELL":
                valid.append(t)
                continue
            pct = t.get("allocation_pct", 0)
            if pct > config.MAX_POSITION_PCT_PER_ASSET * 100:
                pct = config.MAX_POSITION_PCT_PER_ASSET * 100
            amount = balance_idr * (pct / 100)
            if amount < 25000:
                if balance_idr >= 25000:
                    min_pct = 25000 / balance_idr * 100
                    pct = min(min_pct, 100)
                    amount = balance_idr * (pct / 100)
                    if amount >= 25000:
                        t["allocation_pct"] = pct
                        valid.append(t)
                continue
            t["allocation_pct"] = pct
            valid.append(t)
        return valid
