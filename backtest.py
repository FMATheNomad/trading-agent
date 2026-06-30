"""
Backtest module — replays indicators.py atas data historis.

Usage:
  python backtest.py --pair btc_idr --days 30 --tf 15

Mengambil data dari endpoint publik Indodax /tradingview/history_v2,
lalu menjalankan logika indicators.py secara sekuensial untuk mensimulasikan
entry/exit berdasarkan sinyal + risk management.
"""

import argparse
import asyncio
import httpx
from data_layer import fetch_ohlcv
from indicators import compute_single
from risk_manager import RiskManager
import config

class BacktestEngine:
    def __init__(self, initial_capital: float = 100_000):
        self.capital = initial_capital
        self.coin_balance = 0.0
        self.position = None
        self.rm = RiskManager()
        self.trades = []

    def run(self, ohlcv: list[dict]):
        for i in range(30, len(ohlcv)):
            window = ohlcv[:i]
            sig = compute_single(window)
            price = float(ohlcv[i - 1]["close"])

            if self.position:
                result = self.rm.check_sl_tp(self.position["price"], price, self.position["side"])
                if result:
                    pnl = (price - self.position["price"]) * self.position["qty"]
                    self.capital += self.position["qty"] * price
                    self.coin_balance = 0
                    self.trades.append({**self.position, "exit_price": price, "pnl": pnl, "reason": result})
                    self.position = None
                    continue

            if self.position is None and sig["raw_signal"] in ("BUY", "SELL"):
                size = self.rm.compute_position_size(self.capital)
                qty = size / price
                fee = self.rm.estimate_fee(size)
                if not self.rm.is_profit_viable(price, qty, sig["raw_signal"]):
                    continue
                self.position = {
                    "side": sig["raw_signal"],
                    "price": price,
                    "qty": qty,
                    "size": size,
                    "fee": fee,
                }
                self.coin_balance = qty
                self.capital -= size

    def report(self):
        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        wins = len([t for t in self.trades if t.get("pnl", 0) > 0])
        losses = len([t for t in self.trades if t.get("pnl", 0) <= 0])
        print(f"Total trades: {len(self.trades)}")
        print(f"Wins: {wins} / Losses: {losses}")
        print(f"Win rate: {wins / len(self.trades) * 100:.1f}%" if self.trades else "N/A")
        print(f"Total PnL: {total_pnl:.2f}")
        print(f"Final capital: {self.capital:.2f}")
        print(f"Return: {(self.capital - 100_000) / 100_000 * 100:.2f}%")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="btc_idr")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--tf", type=int, default=15)
    args = parser.parse_args()

    config.PAIR = args.pair
    config.SYMBOL = args.pair.replace("_", "").upper()

    async with httpx.AsyncClient() as client:
        ohlcv = await fetch_ohlcv(client, tf=args.tf, limit=args.days * 24 * 4)

    engine = BacktestEngine()
    engine.run(ohlcv)
    engine.report()

if __name__ == "__main__":
    asyncio.run(main())
