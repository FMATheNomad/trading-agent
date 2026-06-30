import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

class CointegrationEngine:
    def __init__(self, z_entry=2.0, z_exit=0.5, min_half_life_hours=2):
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.min_half_life_hours = min_half_life_hours
        self.registry = {}

    def _adf_test(self, series: np.ndarray, critical: float = -2.89) -> bool:
        if len(series) < 30:
            return False
        try:
            result = adfuller(series, autolag="AIC", maxlag=20)
            return result[0] < result[4]["5%"]
        except Exception:
            return False

    def _half_life(self, spread: np.ndarray) -> float:
        if len(spread) < 20:
            return float("inf")
        spread_shifted = spread[:-1]
        spread_lagged = spread[1:]
        try:
            spread_shifted_c = add_constant(spread_shifted)
            model = OLS(spread_lagged, spread_shifted_c).fit()
            theta = model.params[1]
            if theta >= 0:
                return float("inf")
            return -np.log(2) / theta
        except Exception:
            return float("inf")

    def _hurst_exponent(self, series: np.ndarray, max_lag: int = 20) -> float:
        lags = range(2, min(max_lag, len(series) // 2))
        tau = []
        for lag in lags:
            diff = np.subtract(series[lag:], series[:-lag])
            tau.append(np.sqrt(np.std(diff)))
        if len(tau) < 2:
            return 0.5
        poly = np.polyfit(np.log(lags), np.log(tau), 1)
        return poly[0] * 2

    def evaluate_pair(self, pair_a: str, pair_b: str, price_a: np.ndarray, price_b: np.ndarray) -> dict:
        min_len = min(len(price_a), len(price_b))
        if min_len < 50:
            return {"pair": f"{pair_a}/{pair_b}", "cointegrated": False, "signal": "HOLD", "z_score": 0}

        pa = price_a[-min_len:].astype(float)
        pb = price_b[-min_len:].astype(float)
        ratio = pa / pb

        try:
            c_res = coint(pa, pb, maxlag=20)
            c_pval = c_res[1]
        except Exception:
            c_pval = 1.0

        spread_series = ratio
        hl = self._half_life(spread_series)
        hurst = self._hurst_exponent(spread_series)

        spread_mean = np.mean(spread_series[-50:])
        spread_std = np.std(spread_series[-50:])
        z = (spread_series[-1] - spread_mean) / spread_std if spread_std > 0 else 0

        coint_status = c_pval < 0.05 and hl < 48 and hurst < 0.45

        result = {
            "pair": f"{pair_a}/{pair_b}",
            "a": pair_a,
            "b": pair_b,
            "cointegrated": bool(coint_status),
            "p_value": round(c_pval, 4),
            "half_life_hours": round(hl, 2),
            "hurst": round(hurst, 3),
            "z_score": round(z, 3),
            "ratio": round(ratio[-1], 4),
            "mean_ratio": round(spread_mean, 4),
            "signal": "HOLD",
            "action_a": "",
            "action_b": "",
            "reason": "",
        }

        if not coint_status and c_pval < 0.10 and hl < 72:
            result["cointegrated"] = "WEAK"
            result["reason"] = f"weak coint(p={c_pval:.3f}, hl={hl:.1f}h)"

        if coint_status or result.get("cointegrated") == "WEAK":
            if z > self.z_entry:
                result["signal"] = "SHORT_SPREAD"
                result["action_a"] = "SELL"
                result["action_b"] = "BUY"
                result["reason"] = f"A overvalued vs B (z={z:.2f}, hl={hl:.1f}h, H={hurst:.2f})"
            elif z < -self.z_entry:
                result["signal"] = "LONG_SPREAD"
                result["action_a"] = "BUY"
                result["action_b"] = "SELL"
                result["reason"] = f"A undervalued vs B (z={z:.2f}, hl={hl:.1f}h, H={hurst:.2f})"
            elif abs(z) < self.z_exit and abs(z) > 0:
                result["signal"] = "CLOSE_SPREAD"

        if not coint_status and abs(z) > 2.5:
            result["signal"] = "WATCH_ONLY"
            result["reason"] = f"z={z:.2f} but not cointegrated (p={c_pval:.4f})"

        return result

    def scan(self, ohlcv_map: dict, correlation_pairs: list[tuple]) -> list[dict]:
        results = []
        for pair_a, pair_b in correlation_pairs:
            ohlcv_a = ohlcv_map.get(pair_a, [])
            ohlcv_b = ohlcv_map.get(pair_b, [])
            if not ohlcv_a or not ohlcv_b:
                continue
            price_a = np.array([c["close"] for c in ohlcv_a], dtype=float)
            price_b = np.array([c["close"] for c in ohlcv_b], dtype=float)
            res = self.evaluate_pair(pair_a, pair_b, price_a, price_b)
            results.append(res)
        return results
