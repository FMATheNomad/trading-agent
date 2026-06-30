import config

class RiskManager:
    def __init__(self):
        self.daily_start_balance = config.INITIAL_CAPITAL_IDR
        self.today_peak = config.INITIAL_CAPITAL_IDR

    def should_stop_trading(self, current_balance_idr: float) -> bool:
        if current_balance_idr < config.DAILY_LOSS_FLOOR_IDR:
            return True
        return False

    def compute_position_size(self, balance_idr: float) -> float:
        raw = balance_idr * config.POSITION_SIZE_PCT
        min_order = 50_000
        return max(raw, min_order)

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
        if side.upper() == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        if pnl_pct <= config.STOP_LOSS_PCT:
            return "SL_HIT"
        if pnl_pct >= config.TAKE_PROFIT_PCT:
            return "TP_HIT"
        return None
