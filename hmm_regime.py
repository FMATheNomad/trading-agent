# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

import numpy as np
from hmmlearn import hmm

class HMMRegimeDetector:
    def __init__(self, n_states=4):
        self.model = hmm.GaussianHMM(n_components=n_states, covariance_type="full", random_state=42, n_iter=100)
        self.state_names = None
        self.trained = False

    def _build_feature_matrix(self, ohlcv_map: dict) -> np.ndarray:
        all_rows = []
        for pair, ohlcv in ohlcv_map.items():
            closes = np.array([c["close"] for c in ohlcv[-60:]], dtype=float)
            highs = np.array([c["high"] for c in ohlcv[-60:]], dtype=float)
            lows = np.array([c["low"] for c in ohlcv[-60:]], dtype=float)
            if len(closes) < 20:
                continue
            log_ret = np.diff(np.log(closes + 1e-10))
            vol_20 = np.std(log_ret[-20:]) * 100
            atr = np.mean(highs[-14:] - lows[-14:]) / closes[-1] * 100 if closes[-1] else 0
            rng = (closes.max() - closes.min()) / closes.min() * 100
            skew = 0
            if len(log_ret) > 5:
                from scipy.stats import skew as sp_skew
                skew = sp_skew(log_ret[-20:]) if len(log_ret) >= 20 else 0
            all_rows.append([log_ret[-1] * 100 if len(log_ret) > 0 else 0, vol_20, atr, rng, skew])
        if not all_rows:
            return np.zeros((1, 5))
        arr = np.array(all_rows)
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
        return arr

    def fit(self, ohlcv_map: dict):
        X = self._build_feature_matrix(ohlcv_map)
        if X.shape[0] < self.model.n_components:
            return
        self.model.fit(X)
        states = self.model.predict(X)
        state_vol_order = {}
        for i in range(self.model.n_components):
            cov = self.model.covars_[i]
            vol_idx = 1
            if cov.ndim == 2:
                state_vol_order[i] = cov[vol_idx, vol_idx]
            else:
                state_vol_order[i] = cov[0, vol_idx, vol_idx]
        sorted_states = sorted(state_vol_order.items(), key=lambda x: x[1])
        self.state_names = {}
        n = len(sorted_states)
        for idx, (state, _) in enumerate(sorted_states):
            if idx == n - 1 and state_vol_order[state] > 0.03:
                self.state_names[state] = "HIGH_VOL"
            elif idx < n // 3:
                self.state_names[state] = "SIDEWAYS"
            elif idx < 2 * n // 3:
                returns_here = [X[i, 0] for i in range(len(states)) if states[i] == state]
                avg_ret = np.mean(returns_here) if returns_here else 0
                self.state_names[state] = "BULL" if avg_ret > 0 else "BEAR"
            else:
                self.state_names[state] = "BULL"
        self.trained = True

    def predict(self, ohlcv_map: dict) -> dict:
        if not self.trained:
            return {"regime": "UNKNOWN", "confidence": 0.0, "probabilities": {}}
        X = self._build_feature_matrix(ohlcv_map)
        if X.shape[0] == 0:
            return {"regime": "UNKNOWN", "confidence": 0.0, "probabilities": {}}
        states = self.model.predict(X)
        from collections import Counter
        state_counts = Counter(states)
        state = state_counts.most_common(1)[0][0]
        state_ratio = state_counts[state] / len(states)
        probs = self.model.predict_proba(X)
        avg_probs = np.mean(probs, axis=0)
        probs_flat = {self.state_names.get(i, f"STATE_{i}"): float(avg_probs[i]) for i in range(self.model.n_components)}
        name = self.state_names.get(state, "UNKNOWN")
        confidence = float(avg_probs[state])
        return {
            "regime": name,
            "confidence": round(confidence, 3),
            "probabilities": probs_flat,
            "state": int(state),
            "state_ratio": round(state_ratio, 3),
        }
