import numpy as np
import pandas as pd
import config

class RiskManager:
    def __init__(self):
        self.daily_start_balance = config.PLAY_CAPITAL_IDR
        self.today_peak = config.PLAY_CAPITAL_IDR
        self.trailing_highs: dict[str, float] = {}
        self._initial_sl_released: dict[str, bool] = {}
        self._pyramid_done: dict[str, bool] = {}
        self.daily_target_hit = False
        self.daily_loss_stopped = False

    def check_daily_limits(self, total_equity: float) -> str | None:
        if total_equity > self.today_peak:
            self.today_peak = total_equity
        if total_equity < config.DAILY_LOSS_FLOOR_IDR and not self.daily_loss_stopped:
            self.daily_loss_stopped = True
            return "DAILY_LOSS_LIMIT"
        if total_equity > config.MIN_ORDER_IDR and self.daily_loss_stopped:
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

    def is_profit_viable(self, entry_price: float, qty: float, side: str, atr_pct: float | None = None) -> bool:
        atr = max(atr_pct or 1.5, 1.5)
        if atr < 1.0:
            atr = 1.5
        target_mult = atr * config.ATR_TP_MULTIPLIER / 100
        if side.upper() == "BUY":
            target = entry_price * (1 + target_mult)
        else:
            target = entry_price * (1 - target_mult)
        gross = abs(target - entry_price) * qty
        fee_roundtrip = (entry_price * qty * config.TAKER_FEE_PCT) + (target * qty * config.TAKER_FEE_PCT)
        if gross <= fee_roundtrip:
            return False
        sl_mult = atr * config.ATR_SL_MULTIPLIER / 100
        loss_if_sl = abs(entry_price * sl_mult) * qty + fee_roundtrip
        rr_ratio = gross / max(loss_if_sl, 1e-6)
        if rr_ratio < 0.6:
            return False
        return True

    def get_sl_tp(self, entry_price: float, side: str, atr_pct: float | None = None) -> tuple[float, float]:
        atr = atr_pct or 1.0
        sl_mult = max(atr * config.ATR_SL_MULTIPLIER, 0.5)
        tp_mult = max(atr * config.ATR_TP_MULTIPLIER, 1.0)
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
            atr_pct = min(max(atr_pct, 0.5), 10)
        return atr_pct

    def check_sl_tp(self, entry_price: float, current_price: float, side: str, pair: str = "", atr_pct: float | None = None) -> str | None:
        if entry_price <= 0:
            return None
        atr = atr_pct or config.ATR_SL_MULTIPLIER
        if side.upper() == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        if config.ROTHSCHILD_ACTIVE:
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
            trail_pct = atr * config.ATR_SL_MULTIPLIER * 0.5 / 100
            tp_pct = atr * config.ATR_TP_MULTIPLIER / 100
            if side.upper() == "BUY":
                if pair and current_price > self.trailing_highs.get(pair, entry_price):
                    self.trailing_highs[pair] = current_price
                if pair and pnl_pct > 0:
                    trail_stop = self.trailing_highs[pair] * (1 - trail_pct)
                    if current_price <= trail_stop:
                        return "TRAILING_SL"
                if pnl_pct >= tp_pct:
                    return "TP_HIT"
            else:
                if pair and current_price < self.trailing_highs.get(pair, entry_price):
                    self.trailing_highs[pair] = current_price
                if pair and pnl_pct > 0:
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

    def compute_allocation(self, score: int, conviction: str, ml_buy_prob: float = 0.5) -> float:
        base = self.optimal_fraction()
        score_boost = min(abs(score) * 0.05, 0.15)
        conv_boost = 0.1 if conviction == "HIGH" else 0
        ml_boost = (ml_buy_prob - 0.5) * 0.3 if ml_buy_prob > 0.5 else 0
        alloc = base + score_boost + conv_boost + ml_boost
        return round(max(min(alloc, config.MAX_KELLY_ALLOC), config.MIN_KELLY_ALLOC), 2)

class PortfolioRiskManager:
    def __init__(self, initial_capital: float = config.PLAY_CAPITAL_IDR):
        self.peak_capital = initial_capital
        self.initial_capital = initial_capital
        self.kelly = KellyCalculator()

    def set_trade_history(self, trades: list[dict]):
        self.kelly.update(trades)

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
            if amount < config.MIN_ORDER_IDR:
                continue
            t["allocation_pct"] = pct
            valid.append(t)
        return valid
