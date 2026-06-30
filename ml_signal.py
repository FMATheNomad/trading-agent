import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

class XGBoostSignal:
    def __init__(self):
        self.model = None
        self.scaler = StandardScaler()
        self.trained = False
        self.feature_cols = []

    def _label_future_return(self, closes: np.ndarray, horizon: int = 5) -> np.ndarray:
        future = np.roll(closes, -horizon)
        future[-horizon:] = closes[-1]
        returns = (future - closes) / closes
        labels = np.zeros(len(closes))
        labels[returns > 0.01] = 1  # BUY
        labels[returns < -0.01] = 2  # SELL
        return labels

    def _extract_features(self, ohlcv: list[dict]) -> pd.DataFrame:
        if len(ohlcv) < 30:
            return pd.DataFrame()
        df = pd.DataFrame(ohlcv)
        df.columns = [c.lower() for c in df.columns]
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        vol = df.get("volume", df.get("vol", pd.Series([0] * len(df)))).astype(float)
        features = pd.DataFrame(index=df.index)
        features["close"] = close
        features["log_ret_1"] = np.log(close / close.shift(1))
        features["log_ret_5"] = np.log(close / close.shift(5))
        features["log_ret_10"] = np.log(close / close.shift(10))
        features["vol_5"] = features["log_ret_1"].rolling(5).std()
        features["vol_10"] = features["log_ret_1"].rolling(10).std()
        features["vol_ratio"] = features["vol_5"] / features["vol_10"].replace(0, np.nan).replace(np.nan, 1)
        features["high_low_ratio"] = high / low
        features["close_position"] = (close - low) / (high - low + 1e-10)
        features["volume_log"] = np.log(vol + 1)
        features["volume_ratio"] = vol / vol.rolling(20).mean().replace(0, np.nan).replace(np.nan, 1)
        features["rsi_14"] = self._rsi(close, 14)
        features["ema_ratio_9_21"] = close.ewm(span=9).mean() / close.ewm(span=21).mean().replace(0, np.nan).replace(np.nan, 1)
        features["ema_ratio_21_50"] = close.ewm(span=21).mean() / close.ewm(span=50).mean().replace(0, np.nan).replace(np.nan, 1)
        features["bb_position"] = (close - close.rolling(20).mean()) / (close.rolling(20).std() * 2 + 1e-10)
        features.fillna(0, inplace=True)
        features.replace([np.inf, -np.inf], 0, inplace=True)
        self.feature_cols = [c for c in features.columns if c != "close"]
        return features

    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan).replace(np.nan, 1)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)

    def train(self, ohlcv_by_pair: dict[str, list[dict]]):
        if not HAS_XGB:
            return
        all_feats = []
        all_labels = []
        for pair, ohlcv in ohlcv_by_pair.items():
            if len(ohlcv) < 50:
                continue
            features = self._extract_features(ohlcv)
            if features.empty:
                continue
            closes = features["close"].values
            labels = self._label_future_return(closes, horizon=5)
            mask = labels > 0
            feat_data = features[self.feature_cols].values
            all_feats.append(feat_data[mask])
            all_labels.append(labels[mask])
        if not all_feats or sum(len(f) for f in all_feats) < 100:
            return
        X = np.vstack(all_feats)
        y = np.concatenate(all_labels) - 1
        X = self.scaler.fit_transform(X)
        dtrain = xgb.DMatrix(X, label=y)
        params = {
            "objective": "multi:softprob",
            "num_class": 2,
            "max_depth": 4,
            "eta": 0.1,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "eval_metric": "mlogloss",
        }
        self.model = xgb.train(params, dtrain, num_boost_round=100, verbose_eval=False)
        self.trained = True

    def predict(self, ohlcv: list[dict]) -> dict:
        if not self.trained or not HAS_XGB:
            return {"ml_buy_prob": 0.5, "ml_sell_prob": 0.5, "ml_direction": "NEUTRAL"}
        features = self._extract_features(ohlcv)
        if features.empty:
            return {"ml_buy_prob": 0.5, "ml_sell_prob": 0.5, "ml_direction": "NEUTRAL"}
        X = self.scaler.transform(features[self.feature_cols].values)[-1:].reshape(1, -1)
        dtest = xgb.DMatrix(X)
        probs = self.model.predict(dtest)[0]
        buy_prob = float(probs[0])
        sell_prob = float(probs[1])
        direction = "NEUTRAL"
        if buy_prob > 0.65:
            direction = "BUY"
        elif sell_prob > 0.65:
            direction = "SELL"
        return {"ml_buy_prob": round(buy_prob, 3), "ml_sell_prob": round(sell_prob, 3), "ml_direction": direction}
