import numpy as np
import pandas as pd
from scipy.stats import skew, kurtosis, entropy
from numpy.fft import fft, fftfreq

class MicrostructureFeatures:
    @staticmethod
    def compute_orderbook_features(orderbook: dict | None) -> dict:
        if not orderbook:
            return {"spread_pct": 0, "depth_imbalance": 0, "micro_pressure": "NEUTRAL"}
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return {"spread_pct": 0, "depth_imbalance": 0, "micro_pressure": "NEUTRAL"}
        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 0
        mid = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100 if mid else 0
        bid_vol = sum(q for _, q in bids[:5])
        ask_vol = sum(q for _, q in asks[:5])
        total = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total if total > 0 else 0
        micro = "BUY" if imbalance > 0.3 else "SELL" if imbalance < -0.3 else "NEUTRAL"
        return {"spread_pct": round(spread_pct, 4), "depth_imbalance": round(imbalance, 3), "micro_pressure": micro}

    @staticmethod
    def compute_temporal_features(closes: np.ndarray) -> dict:
        if len(closes) < 30:
            return {}
        log_ret = np.diff(np.log(closes + 1e-10))
        n = len(log_ret)
        skew_val = float(skew(log_ret[-n:])) if n > 3 else 0
        kurt_val = float(kurtosis(log_ret[-n:])) if n > 3 else 0
        try:
            fft_vals = fft(closes[-64:] - np.mean(closes[-64:]))
            fft_freqs = fftfreq(len(fft_vals))
            pos_mask = fft_freqs > 0
            power = np.abs(fft_vals[pos_mask])
            dom_freq = float(fft_freqs[pos_mask][np.argmax(power)]) if len(power) > 0 else 0
        except Exception:
            dom_freq = 0
        return {
            "skew": round(skew_val, 3),
            "kurtosis": round(kurt_val, 3),
            "dominant_freq": round(dom_freq, 4),
            "non_gaussian": int(abs(skew_val) > 1 or abs(kurt_val) > 3),
        }

    @staticmethod
    def compute_entropy(closes: np.ndarray, n_bins: int = 10) -> dict:
        if len(closes) < 30:
            return {}
        rets = np.diff(np.log(closes + 1e-10))
        hist, _ = np.histogram(rets, bins=n_bins, density=True)
        hist = hist[hist > 0]
        entropy_val = float(entropy(hist)) if len(hist) > 0 else 0
        perm_entropy = 0
        if len(rets) >= 10:
            try:
                from math import factorial
                m = 3
                patterns = []
                for i in range(len(rets) - m + 1):
                    pattern = np.argsort(rets[i:i+m])
                    patterns.append(tuple(pattern))
                unique = len(set(patterns))
                total = len(patterns)
                perm_entropy = -np.log(unique / total) if total > 0 else 0
            except Exception:
                perm_entropy = 0
        return {"shannon_entropy": round(entropy_val, 3), "perm_entropy": round(perm_entropy, 3)}

    @staticmethod
    def compute_vpin(ohlcv: list[dict], window: int = 20) -> float:
        if len(ohlcv) < window + 1:
            return 0.5
        df = pd.DataFrame(ohlcv[-window-1:])
        df.columns = [c.lower() for c in df.columns]
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        vol = df.get("volume", df.get("vol", pd.Series([0] * len(df)))).astype(float)
        if vol.sum() == 0:
            return 0.5
        delta_p = close - close.shift(1)
        buy_vol = vol * (delta_p > 0).astype(int)
        sell_vol = vol * (delta_p < 0).astype(int)
        vpin = (buy_vol.sum() - sell_vol.sum()).abs() / vol.sum()
        return round(float(vpin), 3)

    @staticmethod
    def compute_all(ohlcv: list[dict], orderbook: dict | None = None) -> dict:
        if len(ohlcv) < 20:
            return {}
        closes = np.array([c["close"] for c in ohlcv], dtype=float)
        result = {}
        result.update(MicrostructureFeatures.compute_temporal_features(closes))
        result.update(MicrostructureFeatures.compute_entropy(closes))
        result["vpin"] = MicrostructureFeatures.compute_vpin(ohlcv)
        if orderbook:
            result.update(MicrostructureFeatures.compute_orderbook_features(orderbook))
        return result
