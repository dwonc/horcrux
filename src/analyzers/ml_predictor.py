"""
Aegis-Trader ML Predictor Module
XGBoost + LSTM 앙상블 예측기
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import accuracy_score, log_loss
from xgboost import XGBClassifier

__model_version__ = "8.1.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class MLPredictorConfig:
    # XGBoost
    xgb_n_estimators: int = 300
    xgb_max_depth: int = 6
    xgb_learning_rate: float = 0.05
    xgb_subsample: float = 0.8

    # LSTM
    lstm_window: int = 60
    lstm_hidden: int = 64
    lstm_layers: int = 2
    lstm_dropout: float = 0.2
    lstm_epochs: int = 50
    lstm_batch_size: int = 64
    lstm_lr: float = 1e-3

    # Ensemble
    xgb_weight: float = 0.6
    lstm_weight: float = 0.4

    # Persistence
    model_dir: str = "models"


# ---------------------------------------------------------------------------
# 22 Technical-Indicator Features
# ---------------------------------------------------------------------------

FEATURE_NAMES = [
    "sma_5", "sma_20", "sma_60",
    "ema_12", "ema_26",
    "rsi_14",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_middle", "bb_lower", "bb_width",
    "atr_14",
    "adx_14",
    "stoch_k", "stoch_d",
    "cci_20",
    "obv",
    "vwap",
    "roc_10",
    "williams_r",
]


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame -> 22 technical indicator columns."""
    out = pd.DataFrame(index=df.index)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # SMA
    out["sma_5"] = close.rolling(5).mean()
    out["sma_20"] = close.rolling(20).mean()
    out["sma_60"] = close.rolling(60).mean()

    # EMA
    out["ema_12"] = close.ewm(span=12, adjust=False).mean()
    out["ema_26"] = close.ewm(span=26, adjust=False).mean()

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi_14"] = 100 - 100 / (1 + rs)

    # MACD
    out["macd"] = out["ema_12"] - out["ema_26"]
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    # Bollinger Bands
    out["bb_middle"] = out["sma_20"]
    bb_std = close.rolling(20).std()
    out["bb_upper"] = out["bb_middle"] + 2 * bb_std
    out["bb_lower"] = out["bb_middle"] - 2 * bb_std
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_middle"]

    # ATR
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    out["atr_14"] = tr.rolling(14).mean()

    # ADX (simplified)
    plus_dm = (high.diff()).clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_di = 100 * (plus_dm.rolling(14).mean() / out["atr_14"])
    minus_di = 100 * (minus_dm.rolling(14).mean() / out["atr_14"])
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out["adx_14"] = dx.rolling(14).mean()

    # Stochastic
    low_14 = low.rolling(14).min()
    high_14 = high.rolling(14).max()
    out["stoch_k"] = 100 * (close - low_14) / (high_14 - low_14).replace(0, np.nan)
    out["stoch_d"] = out["stoch_k"].rolling(3).mean()

    # CCI
    tp = (high + low + close) / 3
    tp_sma = tp.rolling(20).mean()
    tp_mad = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    out["cci_20"] = (tp - tp_sma) / (0.015 * tp_mad)

    # OBV
    obv = (np.sign(close.diff()).fillna(0) * volume).cumsum()
    out["obv"] = obv

    # VWAP (intraday proxy: cumulative)
    cum_vol = volume.cumsum()
    cum_vp = (close * volume).cumsum()
    out["vwap"] = cum_vp / cum_vol.replace(0, np.nan)

    # ROC
    out["roc_10"] = close.pct_change(10) * 100

    # Williams %R
    out["williams_r"] = -100 * (high_14 - close) / (high_14 - low_14).replace(0, np.nan)

    return out[FEATURE_NAMES]


def make_labels(close: pd.Series, horizon: int = 5, threshold: float = 0.0) -> pd.Series:
    """Future return label: 1 (up), 0 (down)."""
    future_ret = close.pct_change(horizon).shift(-horizon)
    return (future_ret > threshold).astype(int)


# ---------------------------------------------------------------------------
# LSTM Model (PyTorch)
# ---------------------------------------------------------------------------

class LSTMNet(nn.Module):
    def __init__(self, input_size: int, hidden_size: int = 64, num_layers: int = 2, dropout: float = 0.2):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size, hidden_size, num_layers,
            batch_first=True, dropout=dropout if num_layers > 1 else 0.0,
        )
        self.fc = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.lstm(x)
        return self.fc(out[:, -1, :]).squeeze(-1)


class LSTMPredictor:
    """Train / predict wrapper for the LSTM model."""

    def __init__(self, cfg: MLPredictorConfig):
        self.cfg = cfg
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model: Optional[LSTMNet] = None
        self._feat_mean: Optional[np.ndarray] = None
        self._feat_std: Optional[np.ndarray] = None

    # -- helpers --
    def _make_sequences(self, features: np.ndarray, labels: Optional[np.ndarray] = None):
        w = self.cfg.lstm_window
        X_seq, y_seq = [], []
        for i in range(w, len(features)):
            X_seq.append(features[i - w: i])
            if labels is not None:
                y_seq.append(labels[i])
        X_seq = np.array(X_seq, dtype=np.float32)
        if labels is not None:
            y_seq = np.array(y_seq, dtype=np.float32)
            return X_seq, y_seq
        return X_seq

    def _normalize(self, features: np.ndarray, fit: bool = False) -> np.ndarray:
        if fit:
            self._feat_mean = np.nanmean(features, axis=0)
            self._feat_std = np.nanstd(features, axis=0) + 1e-8
        return (features - self._feat_mean) / self._feat_std

    # -- public API --
    def fit(self, features: np.ndarray, labels: np.ndarray):
        features = self._normalize(features, fit=True)
        X, y = self._make_sequences(features, labels)
        dataset = torch.utils.data.TensorDataset(
            torch.from_numpy(X), torch.from_numpy(y),
        )
        loader = torch.utils.data.DataLoader(
            dataset, batch_size=self.cfg.lstm_batch_size, shuffle=True,
        )
        self.model = LSTMNet(
            X.shape[2], self.cfg.lstm_hidden, self.cfg.lstm_layers, self.cfg.lstm_dropout,
        ).to(self.device)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.cfg.lstm_lr)
        criterion = nn.BCEWithLogitsLoss()

        self.model.train()
        for epoch in range(self.cfg.lstm_epochs):
            total_loss = 0.0
            for xb, yb in loader:
                xb, yb = xb.to(self.device), yb.to(self.device)
                optimizer.zero_grad()
                loss = criterion(self.model(xb), yb)
                loss.backward()
                optimizer.step()
                total_loss += loss.item() * len(xb)
            if (epoch + 1) % 10 == 0:
                logger.info("LSTM epoch %d/%d  loss=%.4f", epoch + 1, self.cfg.lstm_epochs, total_loss / len(dataset))

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        """Return P(up) for each row that has a full window."""
        features = self._normalize(features, fit=False)
        X = self._make_sequences(features)
        self.model.eval()
        with torch.no_grad():
            logits = self.model(torch.from_numpy(X).to(self.device))
            probs = torch.sigmoid(logits).cpu().numpy()
        return probs


# ---------------------------------------------------------------------------
# Ensemble Predictor
# ---------------------------------------------------------------------------

class MLPredictor:
    """XGBoost + LSTM 앙상블 예측기."""

    def __init__(self, cfg: Optional[MLPredictorConfig] = None):
        self.cfg = cfg or MLPredictorConfig()
        self.xgb = XGBClassifier(
            n_estimators=self.cfg.xgb_n_estimators,
            max_depth=self.cfg.xgb_max_depth,
            learning_rate=self.cfg.xgb_learning_rate,
            subsample=self.cfg.xgb_subsample,
            use_label_encoder=False,
            eval_metric="logloss",
        )
        self.lstm = LSTMPredictor(self.cfg)
        self._is_fitted = False

    def fit(self, df: pd.DataFrame, label_horizon: int = 5):
        """OHLCV DataFrame으로 학습."""
        features = compute_features(df)
        labels = make_labels(df["close"], horizon=label_horizon)

        # Drop NaN rows (from rolling windows)
        valid = features.notna().all(axis=1) & labels.notna()
        feat_arr = features.loc[valid].values
        label_arr = labels.loc[valid].values

        logger.info("Training on %d samples, %d features", len(feat_arr), feat_arr.shape[1])

        # XGBoost
        self.xgb.fit(feat_arr, label_arr)
        logger.info("XGBoost training complete")

        # LSTM
        self.lstm.fit(feat_arr, label_arr)
        logger.info("LSTM training complete")

        self._is_fitted = True

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        """Return ensemble P(up) aligned to df index (NaN-padded)."""
        assert self._is_fitted, "Call fit() first"
        features = compute_features(df)
        valid = features.notna().all(axis=1)
        feat_arr = features.loc[valid].values

        # XGBoost probabilities
        xgb_prob = self.xgb.predict_proba(feat_arr)[:, 1]

        # LSTM probabilities (shorter due to window)
        lstm_prob = self.lstm.predict_proba(feat_arr)
        w = self.cfg.lstm_window

        # Align: LSTM output maps to feat_arr[w:]
        result = np.full(len(df), np.nan)
        valid_idx = np.where(valid.values)[0]

        # XGBoost covers all valid rows; LSTM covers valid[w:]
        for i, gi in enumerate(valid_idx):
            lstm_i = i - w
            if lstm_i >= 0 and lstm_i < len(lstm_prob):
                result[gi] = (
                    self.cfg.xgb_weight * xgb_prob[i]
                    + self.cfg.lstm_weight * lstm_prob[lstm_i]
                )
            else:
                result[gi] = xgb_prob[i]  # XGBoost only

        return result

    def predict_signal(self, df: pd.DataFrame) -> np.ndarray:
        """Return signal: 1 (buy), -1 (sell), 0 (hold). Thresholds: >0.6 buy, <0.4 sell."""
        proba = self.predict_proba(df)
        signal = np.zeros_like(proba)
        signal[proba > 0.6] = 1
        signal[proba < 0.4] = -1
        return signal

    # -----------------------------------------------------------------------
    # scorer_adapter: 기존 score 시스템과 통합
    # -----------------------------------------------------------------------
    def scorer_adapter(self, symbol: str, df: pd.DataFrame) -> float:
        """
        기존 scoring 파이프라인과 호환되는 어댑터.
        Returns: float in [-1, 1] — 최근 봉 기준 앙상블 신호.
        """
        proba = self.predict_proba(df)
        last_valid = proba[~np.isnan(proba)]
        if len(last_valid) == 0:
            return 0.0
        # Map [0,1] -> [-1,1]
        return float(last_valid[-1] * 2 - 1)

    # -----------------------------------------------------------------------
    # Persistence: save / load
    # -----------------------------------------------------------------------
    def save(self, tag: str = "default"):
        """Save XGBoost model, LSTM weights, and normalization stats."""
        model_dir = Path(self.cfg.model_dir) / tag
        model_dir.mkdir(parents=True, exist_ok=True)

        # XGBoost
        self.xgb.save_model(str(model_dir / "xgb_model.json"))

        # LSTM
        if self.lstm.model is not None:
            torch.save(self.lstm.model.state_dict(), str(model_dir / "lstm_weights.pt"))

        # Normalization stats + config
        meta = {
            "feat_mean": self.lstm._feat_mean.tolist() if self.lstm._feat_mean is not None else None,
            "feat_std": self.lstm._feat_std.tolist() if self.lstm._feat_std is not None else None,
            "config": {
                "lstm_window": self.cfg.lstm_window,
                "lstm_hidden": self.cfg.lstm_hidden,
                "lstm_layers": self.cfg.lstm_layers,
                "lstm_dropout": self.cfg.lstm_dropout,
                "xgb_weight": self.cfg.xgb_weight,
                "lstm_weight": self.cfg.lstm_weight,
            },
        }
        (model_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        logger.info("Models saved to %s", model_dir)

    def load(self, tag: str = "default"):
        """Load saved models."""
        model_dir = Path(self.cfg.model_dir) / tag
        assert model_dir.exists(), f"Model directory not found: {model_dir}"

        # Meta
        meta = json.loads((model_dir / "meta.json").read_text())
        if meta["feat_mean"] is not None:
            self.lstm._feat_mean = np.array(meta["feat_mean"])
            self.lstm._feat_std = np.array(meta["feat_std"])

        cfg = meta["config"]
        self.cfg.lstm_window = cfg["lstm_window"]
        self.cfg.lstm_hidden = cfg["lstm_hidden"]
        self.cfg.lstm_layers = cfg["lstm_layers"]
        self.cfg.lstm_dropout = cfg["lstm_dropout"]
        self.cfg.xgb_weight = cfg["xgb_weight"]
        self.cfg.lstm_weight = cfg["lstm_weight"]

        # XGBoost
        self.xgb.load_model(str(model_dir / "xgb_model.json"))

        # LSTM
        n_features = len(FEATURE_NAMES)
        self.lstm.model = LSTMNet(
            n_features, self.cfg.lstm_hidden, self.cfg.lstm_layers, self.cfg.lstm_dropout,
        ).to(self.lstm.device)
        self.lstm.model.load_state_dict(
            torch.load(str(model_dir / "lstm_weights.pt"), map_location=self.lstm.device, weights_only=True)
        )
        self.lstm.model.eval()

        self._is_fitted = True
        logger.info("Models loaded from %s", model_dir)

    # -----------------------------------------------------------------------
    # Walk-Forward Validation
    # -----------------------------------------------------------------------
    def walk_forward_validate(self, df: pd.DataFrame, n_splits: int = 5) -> dict:
        """
        확장 윈도우 기반 워크포워드 검증.
        과거 데이터로 학습, 미래 폴드로 테스트.

        Returns:
            dict with per-fold accuracy, overall accuracy, Sharpe-like metric
        """
        features = compute_features(df)
        labels = make_labels(df["close"])
        valid = features.notna().all(axis=1) & labels.notna()
        feat_arr = features.loc[valid].values
        label_arr = labels.loc[valid].values

        n = len(feat_arr)
        fold_size = n // (n_splits + 1)  # 첫 fold_size는 최소 학습 데이터

        fold_accuracies: list[float] = []
        fold_returns: list[float] = []

        for fold in range(n_splits):
            train_end = fold_size * (fold + 1)
            test_end = min(train_end + fold_size, n)
            if train_end >= n or test_end <= train_end:
                break

            X_train, y_train = feat_arr[:train_end], label_arr[:train_end]
            X_test, y_test = feat_arr[train_end:test_end], label_arr[train_end:test_end]

            # XGBoost만 사용 (LSTM은 시퀀스 윈도우 때문에 폴드별 재학습 비용이 큼)
            xgb_fold = XGBClassifier(
                n_estimators=self.cfg.xgb_n_estimators,
                max_depth=self.cfg.xgb_max_depth,
                learning_rate=self.cfg.xgb_learning_rate,
                subsample=self.cfg.xgb_subsample,
                use_label_encoder=False,
                eval_metric="logloss",
            )
            xgb_fold.fit(X_train, y_train)

            preds = xgb_fold.predict(X_test)
            proba = xgb_fold.predict_proba(X_test)[:, 1]

            acc = accuracy_score(y_test, preds)
            fold_accuracies.append(acc)

            # Sharpe-like: 시그널 수익률의 평균/표준편차
            signals = np.where(proba > 0.5, 1.0, -1.0)
            pseudo_returns = signals * (y_test * 2 - 1)  # +1 맞으면, -1 틀리면
            if pseudo_returns.std() > 0:
                sharpe = pseudo_returns.mean() / pseudo_returns.std() * np.sqrt(252)
            else:
                sharpe = 0.0
            fold_returns.append(sharpe)

            logger.info(
                "Walk-forward fold %d/%d  train=%d  test=%d  acc=%.4f  sharpe=%.2f",
                fold + 1, n_splits, train_end, test_end - train_end, acc, sharpe,
            )

        overall_accuracy = float(np.mean(fold_accuracies)) if fold_accuracies else 0.0
        overall_sharpe = float(np.mean(fold_returns)) if fold_returns else 0.0

        result = {
            "n_splits": len(fold_accuracies),
            "fold_accuracies": fold_accuracies,
            "overall_accuracy": overall_accuracy,
            "overall_sharpe": overall_sharpe,
        }
        logger.info(
            "Walk-forward 완료: overall_acc=%.4f  overall_sharpe=%.2f",
            overall_accuracy, overall_sharpe,
        )
        return result

    # -----------------------------------------------------------------------
    # Feature Importance
    # -----------------------------------------------------------------------
    def get_feature_importance(self) -> dict[str, float]:
        """XGBoost feature importance를 dict로 반환."""
        assert self._is_fitted, "Call fit() first"
        importances = self.xgb.feature_importances_
        return {
            name: float(score)
            for name, score in zip(FEATURE_NAMES, importances)
        }

    # -----------------------------------------------------------------------
    # Adaptive Ensemble Weights
    # -----------------------------------------------------------------------
    def optimize_weights(self, df: pd.DataFrame, val_ratio: float = 0.2) -> dict:
        """
        검증 세트에서 XGBoost/LSTM 가중치 최적 조합 탐색.
        log-loss를 최소화하는 가중치 조합을 선택하여 self.cfg에 반영.

        Returns:
            dict with best weights and validation metrics
        """
        assert self._is_fitted, "Call fit() first"

        features = compute_features(df)
        labels = make_labels(df["close"])
        valid = features.notna().all(axis=1) & labels.notna()
        feat_arr = features.loc[valid].values
        label_arr = labels.loc[valid].values

        # XGBoost 확률
        xgb_prob = self.xgb.predict_proba(feat_arr)[:, 1]

        # LSTM 확률 (윈도우 오프셋 적용)
        lstm_prob_raw = self.lstm.predict_proba(feat_arr)
        w = self.cfg.lstm_window

        # LSTM 결과와 정렬된 구간만 사용 (feat_arr[w:] 에 해당)
        xgb_aligned = xgb_prob[w:]
        lstm_aligned = lstm_prob_raw[:len(xgb_aligned)]
        labels_aligned = label_arr[w:]

        n = len(xgb_aligned)
        val_start = int(n * (1 - val_ratio))
        xgb_val = xgb_aligned[val_start:]
        lstm_val = lstm_aligned[val_start:]
        labels_val = labels_aligned[val_start:]

        if len(labels_val) < 10:
            logger.warning("검증 데이터가 너무 적음 (%d samples), 가중치 최적화 건너뜀", len(labels_val))
            return {"xgb_weight": self.cfg.xgb_weight, "lstm_weight": self.cfg.lstm_weight}

        # 0.0 ~ 1.0, 0.05 단위로 탐색
        best_loss = float("inf")
        best_xgb_w = self.cfg.xgb_weight

        for xgb_w_int in range(0, 21):  # 0, 1, ..., 20 -> 0.0, 0.05, ..., 1.0
            xgb_w = xgb_w_int * 0.05
            lstm_w = 1.0 - xgb_w
            ensemble_prob = xgb_w * xgb_val + lstm_w * lstm_val
            ensemble_prob = np.clip(ensemble_prob, 1e-7, 1 - 1e-7)
            try:
                loss = log_loss(labels_val, ensemble_prob)
            except ValueError:
                continue
            if loss < best_loss:
                best_loss = loss
                best_xgb_w = xgb_w

        best_lstm_w = 1.0 - best_xgb_w
        self.cfg.xgb_weight = round(best_xgb_w, 2)
        self.cfg.lstm_weight = round(best_lstm_w, 2)

        result = {
            "xgb_weight": self.cfg.xgb_weight,
            "lstm_weight": self.cfg.lstm_weight,
            "val_logloss": float(best_loss),
            "val_samples": len(labels_val),
        }
        logger.info(
            "가중치 최적화 완료: xgb=%.2f  lstm=%.2f  val_logloss=%.4f",
            self.cfg.xgb_weight, self.cfg.lstm_weight, best_loss,
        )
        return result

    # -----------------------------------------------------------------------
    # Unified predict()
    # -----------------------------------------------------------------------
    def predict(self, df: pd.DataFrame) -> dict:
        """
        통합 예측 메서드. 마지막 봉 기준 신호, 확률, 피처 중요도 등 반환.

        Returns:
            dict with signal, confidence, probabilities, feature importance, meta
        """
        assert self._is_fitted, "Call fit() first"

        features = compute_features(df)
        valid = features.notna().all(axis=1)
        feat_arr = features.loc[valid].values

        # XGBoost
        xgb_prob = self.xgb.predict_proba(feat_arr)[:, 1]

        # LSTM
        lstm_prob_raw = self.lstm.predict_proba(feat_arr)
        w = self.cfg.lstm_window

        # 마지막 유효 인덱스 기준 앙상블
        last_xgb = float(xgb_prob[-1]) if len(xgb_prob) > 0 else 0.5
        last_lstm_idx = len(xgb_prob) - 1 - w
        if last_lstm_idx >= 0 and last_lstm_idx < len(lstm_prob_raw):
            last_lstm = float(lstm_prob_raw[last_lstm_idx])
            ensemble_p = self.cfg.xgb_weight * last_xgb + self.cfg.lstm_weight * last_lstm
        else:
            last_lstm = float("nan")
            ensemble_p = last_xgb

        # 시그널 결정
        if ensemble_p > 0.6:
            signal = 1
        elif ensemble_p < 0.4:
            signal = -1
        else:
            signal = 0

        # Confidence: 0.5로부터의 거리 * 2 -> [0, 1]
        confidence = min(abs(ensemble_p - 0.5) * 2, 1.0)

        # Feature importance (top 5)
        full_importance = self.get_feature_importance()
        top5 = dict(sorted(full_importance.items(), key=lambda x: x[1], reverse=True)[:5])

        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "ensemble_proba": round(ensemble_p, 6),
            "xgb_proba": round(last_xgb, 6),
            "lstm_proba": round(last_lstm, 6) if not np.isnan(last_lstm) else None,
            "feature_importance": top5,
            "meta": {
                "model_version": __model_version__,
                "n_features": len(FEATURE_NAMES),
                "is_calibrated": False,
            },
        }


# ---------------------------------------------------------------------------
# CalibratedPredictor — Platt Scaling 보정 래퍼
# ---------------------------------------------------------------------------

class CalibratedPredictor:
    """
    MLPredictor의 XGBoost 출력에 Platt scaling을 적용하는 보정 래퍼.
    sklearn의 CalibratedClassifierCV를 사용.
    """

    def __init__(self, base_predictor: MLPredictor):
        self.base = base_predictor
        self.calibrated_xgb: Optional[CalibratedClassifierCV] = None
        self._is_calibrated = False

    def calibrate(self, df: pd.DataFrame, method: str = "sigmoid", cv: int = 3) -> None:
        """
        보정 데이터로 Platt scaling 적용.

        Args:
            df: OHLCV DataFrame (보정용 데이터)
            method: 'sigmoid' (Platt scaling) 또는 'isotonic'
            cv: 교차 검증 폴드 수
        """
        assert self.base._is_fitted, "Base predictor must be fitted first"

        features = compute_features(df)
        labels = make_labels(df["close"])
        valid = features.notna().all(axis=1) & labels.notna()
        feat_arr = features.loc[valid].values
        label_arr = labels.loc[valid].values

        self.calibrated_xgb = CalibratedClassifierCV(
            estimator=self.base.xgb,
            method=method,
            cv=cv,
        )
        self.calibrated_xgb.fit(feat_arr, label_arr)
        self._is_calibrated = True
        logger.info("XGBoost 보정 완료 (method=%s, cv=%d, samples=%d)", method, cv, len(feat_arr))

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        """보정된 앙상블 확률 반환. 보정 안 됐으면 기본 predictor 사용."""
        if not self._is_calibrated:
            logger.warning("보정되지 않음, 기본 predictor 사용")
            return self.base.predict_proba(df)

        features = compute_features(df)
        valid = features.notna().all(axis=1)
        feat_arr = features.loc[valid].values

        # 보정된 XGBoost 확률
        xgb_prob = self.calibrated_xgb.predict_proba(feat_arr)[:, 1]

        # LSTM 확률 (보정 없음 — LSTM은 sigmoid 출력이므로 이미 확률적)
        lstm_prob = self.base.lstm.predict_proba(feat_arr)
        w = self.base.cfg.lstm_window

        result = np.full(len(df), np.nan)
        valid_idx = np.where(valid.values)[0]

        for i, gi in enumerate(valid_idx):
            lstm_i = i - w
            if 0 <= lstm_i < len(lstm_prob):
                result[gi] = (
                    self.base.cfg.xgb_weight * xgb_prob[i]
                    + self.base.cfg.lstm_weight * lstm_prob[lstm_i]
                )
            else:
                result[gi] = xgb_prob[i]

        return result

    def predict(self, df: pd.DataFrame) -> dict:
        """보정된 통합 예측. MLPredictor.predict()와 동일한 구조 반환."""
        assert self.base._is_fitted, "Base predictor must be fitted first"

        features = compute_features(df)
        valid = features.notna().all(axis=1)
        feat_arr = features.loc[valid].values

        # XGBoost (보정 여부에 따라)
        if self._is_calibrated:
            xgb_prob = self.calibrated_xgb.predict_proba(feat_arr)[:, 1]
        else:
            xgb_prob = self.base.xgb.predict_proba(feat_arr)[:, 1]

        lstm_prob_raw = self.base.lstm.predict_proba(feat_arr)
        w = self.base.cfg.lstm_window

        last_xgb = float(xgb_prob[-1]) if len(xgb_prob) > 0 else 0.5
        last_lstm_idx = len(xgb_prob) - 1 - w
        if 0 <= last_lstm_idx < len(lstm_prob_raw):
            last_lstm = float(lstm_prob_raw[last_lstm_idx])
            ensemble_p = self.base.cfg.xgb_weight * last_xgb + self.base.cfg.lstm_weight * last_lstm
        else:
            last_lstm = float("nan")
            ensemble_p = last_xgb

        if ensemble_p > 0.6:
            signal = 1
        elif ensemble_p < 0.4:
            signal = -1
        else:
            signal = 0

        confidence = min(abs(ensemble_p - 0.5) * 2, 1.0)

        full_importance = self.base.get_feature_importance()
        top5 = dict(sorted(full_importance.items(), key=lambda x: x[1], reverse=True)[:5])

        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "ensemble_proba": round(ensemble_p, 6),
            "xgb_proba": round(last_xgb, 6),
            "lstm_proba": round(last_lstm, 6) if not np.isnan(last_lstm) else None,
            "feature_importance": top5,
            "meta": {
                "model_version": __model_version__,
                "n_features": len(FEATURE_NAMES),
                "is_calibrated": self._is_calibrated,
            },
        }

    def save(self, tag: str = "default") -> None:
        """기본 모델 + 보정 모델 저장."""
        self.base.save(tag)
        if self._is_calibrated and self.calibrated_xgb is not None:
            model_dir = Path(self.base.cfg.model_dir) / tag
            with open(model_dir / "calibrated_xgb.pkl", "wb") as f:
                pickle.dump(self.calibrated_xgb, f)
            logger.info("보정 모델 저장 완료: %s", model_dir / "calibrated_xgb.pkl")

    def load(self, tag: str = "default") -> None:
        """기본 모델 + 보정 모델 로드."""
        self.base.load(tag)
        cal_path = Path(self.base.cfg.model_dir) / tag / "calibrated_xgb.pkl"
        if cal_path.exists():
            with open(cal_path, "rb") as f:
                self.calibrated_xgb = pickle.load(f)
            self._is_calibrated = True
            logger.info("보정 모델 로드 완료: %s", cal_path)
        else:
            self._is_calibrated = False
            logger.info("보정 모델 없음, 기본 predictor만 로드됨")
