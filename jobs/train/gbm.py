"""LightGBM 三分类训练 + isotonic 校准 + 融合权重（《02》§5.2/§5.3）。

- 超参固定：num_leaves=15 / min_data_in_leaf=40 / lr=0.05 / n_estimators≤400，早停
- 类别不平衡：balanced 权重（n / (3 × count_c)）
- 校准：最近 60 个样本外交易日上逐类 one-vs-rest isotonic，再归一化
- 融合：w_A, w_B ∝ exp(-logloss)（最近 60 样本外），公式固定不人工干预
"""

from __future__ import annotations

import math
import pickle
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
from sklearn.isotonic import IsotonicRegression

LGBM_PARAMS: dict[str, Any] = {
    "objective": "multiclass",
    "num_class": 3,
    "num_leaves": 15,
    "min_data_in_leaf": 40,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "verbose": -1,
    "seed": 42,
}
MAX_ROUNDS = 400
EARLY_STOPPING_ROUNDS = 30

N_CLASSES = 3


def class_weights(y: list[int]) -> list[float]:
    """balanced 类别权重（索引即类别 0/1/2）。"""
    counts = [0] * N_CLASSES
    for v in y:
        counts[v] += 1
    n = len(y)
    return [n / (N_CLASSES * c) if c else 0.0 for c in counts]


def train_booster(
    x_train: list[list[float]],
    y_train: list[int],
    x_valid: list[list[float]],
    y_valid: list[int],
    feature_names: list[str],
) -> lgb.Booster:
    """带早停的三分类 GBM；样本按类别平衡加权。"""
    weights = class_weights(y_train)
    dtrain = lgb.Dataset(
        np.asarray(x_train, dtype=np.float64),
        label=y_train,
        weight=[weights[v] for v in y_train],
        feature_name=feature_names,
        free_raw_data=False,
    )
    dvalid = lgb.Dataset(
        np.asarray(x_valid, dtype=np.float64),
        label=y_valid,
        reference=dtrain,
        free_raw_data=False,
    )
    booster = lgb.train(
        LGBM_PARAMS,
        dtrain,
        num_boost_round=MAX_ROUNDS,
        valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    return booster


def predict_proba(booster: lgb.Booster, x: list[list[float]]) -> list[list[float]]:
    """返回每行 [p_down, p_flat, p_up]（列序 = 类别 0/1/2）。"""
    raw = booster.predict(np.asarray(x, dtype=np.float64))
    return [[float(v) for v in row] for row in raw]


class IsotonicCalibrator:
    """逐类 one-vs-rest isotonic 校准，输出重新归一化的三类概率。"""

    def __init__(self) -> None:
        self._models: dict[int, IsotonicRegression] = {}

    def fit(self, probas: list[list[float]], y: list[int]) -> IsotonicCalibrator:
        for c in range(N_CLASSES):
            iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            iso.fit([p[c] for p in probas], [1.0 if v == c else 0.0 for v in y])
            self._models[c] = iso
        return self

    def transform(self, probas: list[list[float]]) -> list[list[float]]:
        out: list[list[float]] = []
        for p in probas:
            cal = [float(self._models[c].predict([p[c]])[0]) for c in range(N_CLASSES)]
            total = sum(cal)
            if total <= 0:
                out.append([1.0 / N_CLASSES] * N_CLASSES)
            else:
                out.append([v / total for v in cal])
        return out

    def dumps(self) -> bytes:
        return pickle.dumps({c: m for c, m in self._models.items()})

    @classmethod
    def loads(cls, payload: bytes) -> IsotonicCalibrator:
        obj = cls()
        obj._models = pickle.loads(payload)  # 本地产物，非外部输入
        return obj


def logloss(probas: list[list[float]], y: list[int]) -> float:
    """多分类 logloss（概率裁剪防 log0）。"""
    eps = 1e-15
    total = 0.0
    for p, v in zip(probas, y, strict=True):
        total += -math.log(min(max(p[v], eps), 1.0))
    return total / max(1, len(y))


def fusion_weights(logloss_rule: float, logloss_gbm: float) -> tuple[float, float]:
    """w ∝ exp(-logloss)，归一化（《02》§5.3）。"""
    wa = math.exp(-logloss_rule)
    wb = math.exp(-logloss_gbm)
    total = wa + wb
    return round(wa / total, 6), round(wb / total, 6)


def save_artifacts(
    model_dir: Path, version: str, key: str, booster: lgb.Booster, calibrator: IsotonicCalibrator
) -> dict[str, str]:
    """落盘 booster 文本与校准器 pickle；返回相对路径。"""
    folder = model_dir / version
    folder.mkdir(parents=True, exist_ok=True)
    booster_path = folder / f"{key}.txt"
    iso_path = folder / f"{key}.iso.pkl"
    booster.save_model(str(booster_path))
    iso_path.write_bytes(calibrator.dumps())
    return {"booster": str(booster_path), "calibrator": str(iso_path)}


def load_artifacts(paths: dict[str, str]) -> tuple[lgb.Booster, IsotonicCalibrator]:
    booster = lgb.Booster(model_file=paths["booster"])
    calibrator = IsotonicCalibrator.loads(Path(paths["calibrator"]).read_bytes())
    return booster, calibrator
