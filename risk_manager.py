import config

class RiskManager:
    def __init__(self):
        self.daily_start_balance = config.PLAY_CAPITAL_IDR
        self.today_peak = config.PLAY_CAPITAL_IDR

    def should_stop_trading(self, total_equity: float) -> bool:
        if total_equity < config.DAILY_LOSS_FLOOR_IDR:
            return True
        return False

    def compute_position_size(self, balance_idr: float) -> float:
        raw = balance_idr * config.POSITION_SIZE_PCT
        return max(raw, config.MIN_ORDER_IDR)

    def estimate_fee(self, amount_idr: float) -> float:
        return amount_idr * config.TAKER_FEE_PCT

    def is_profit_viable(self, entry_price: float, qty: float, side: str) -> bool:
        if side.upper() == "BUY":
            target = entry_price * (1 + abs(config.TAKE_PROFIT_PCT))
        else:
            target = entry_price * (1 - abs(config.TAKE_PROFIT_PCT))
        gross = abs(target - entry_price) * qty
        fee_roundtrip = (entry_price * qty * config.TAKER_FEE_PCT) + (target * qty * config.TAKER_FEE_PCT)
        return gross > fee_roundtrip

    def get_sl_tp(self, entry_price: float, side: str) -> tuple[float, float]:
        if side.upper() == "BUY":
            sl = entry_price * (1 + config.STOP_LOSS_PCT)
            tp = entry_price * (1 + config.TAKE_PROFIT_PCT)
        else:
            sl = entry_price * (1 - config.STOP_LOSS_PCT)
            tp = entry_price * (1 - config.TAKE_PROFIT_PCT)
        return round(sl, 2), round(tp, 2)

    def check_sl_tp(self, entry_price: float, current_price: float, side: str) -> str | None:
        if entry_price <= 0:
            return None
        if side.upper() == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
        if pnl_pct <= config.STOP_LOSS_PCT:
            return "SL_HIT"
        if pnl_pct >= config.TAKE_PROFIT_PCT:
            return "TP_HIT"
        return None


class PortfolioRiskManager:
    def __init__(self, initial_capital: float = config.PLAY_CAPITAL_IDR):
        self.peak_capital = initial_capital
        self.initial_capital = initial_capital

    def check_portfolio_stop(self, total_equity: float) -> bool:
        if total_equity > self.peak_capital:
            self.peak_capital = total_equity
        drawdown = (self.peak_capital - total_equity) / self.peak_capital
        if drawdown >= abs(config.PORTFOLIO_STOP_LOSS_PCT):
            return True
        return False

    def validate_allocation(self, trades: list[dict], current_positions: list[dict],
                             balance_idr: float) -> list[dict]:
        valid = []
        for t in trades:
            pct = t.get("allocation_pct", 0)
            if pct > config.MAX_POSITION_PCT_PER_ASSET * 100:
                pct = config.MAX_POSITION_PCT_PER_ASSET * 100
            amount = balance_idr * (pct / 100)
            if amount < config.MIN_ORDER_IDR:
                continue
            t["allocation_pct"] = pct
            valid.append(t)
        return valid
