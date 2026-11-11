"""M4 训练流水线：特征矩阵 → LightGBM → isotonic 校准 → 融合权重 → 样本外评估 → registry。

切分协议（防泄漏，与《02》§6.1 一致）：

    |←—— train（末尾 20% 作早停验证）——→|← 校准 60 →|← 永久 holdout 最近 60 交易日 →|

- 校准窗：拟合 isotonic 与融合权重（w ∝ exp(-logloss)），不碰 holdout
- holdout：只做一次最终评估，对照组为同一批样本上的规则基线
- 上线门槛（DoD）：holdout Balanced Acc ≥ 基线+0.03 且 brier_direction ≤ 0.23 且 ECE ≤ 0.05，
  达标 → active（旧 active 自动 retired），否则 → shadow
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lightgbm as lgb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobs.predict.fusion import direction_from_probs, fuse_probs
from jobs.predict.metrics import BacktestRecord, summarize
from jobs.train.dataset import (
    Sample,
    base_feature_names,
    load_samples,
    pooled_feature_names,
    to_matrix,
)
from jobs.train.gbm import (
    IsotonicCalibrator,
    fusion_weights,
    logloss,
    predict_proba,
    save_artifacts,
    train_booster,
)

logger = logging.getLogger(__name__)

MIN_TRAIN_SAMPLES = 300  # 不足则板块池化（《02》§5.2）
CALIB_WINDOW = 60
HOLDOUT_DAYS = 60
MIN_CALIB_SAMPLES = 30  # 校准窗都不足 → 该品种不挂 GBM（纯规则兜底）

# DoD 门槛（《02》§5.4/§6.3）
GATE_BALANCED_ACC_DELTA = 0.03
GATE_BRIER_DIRECTION = 0.23
GATE_ECE = 0.05


@dataclass(slots=True)
class VarietyModel:
    code: str
    artifact: dict[str, str]
    pooled: bool
    w_rule: float
    w_gbm: float
    importance: dict[str, float]
    booster: lgb.Booster = field(repr=False)
    calibrator: IsotonicCalibrator = field(repr=False)


def _records(
    samples: list[Sample], probs: list[list[float]], dirs: list[int], confs: list[float]
) -> list[BacktestRecord]:
    actual_dir = {0: -1, 1: 0, 2: 1}
    return [
        BacktestRecord(
            variety_code=s.variety_code,
            trade_date=s.trade_date,
            pred_dir=d,
            pred_bucket="",
            p_up=p[2],
            p_flat=p[1],
            p_down=p[0],
            confidence=c,
            actual_change=s.actual_change,
            actual_dir=actual_dir[s.label],
            actual_bucket="",
            transmission_sign=0,
            max_abs_foreign=0.0,
        )
        for s, p, d, c in zip(samples, probs, dirs, confs, strict=True)
    ]


def _rule_records(samples: list[Sample]) -> list[BacktestRecord]:
    return _records(
        samples,
        [list(s.rule_p) for s in samples],
        [s.rule_dir for s in samples],
        [s.rule_confidence for s in samples],
    )


def _split(samples: list[Sample]) -> tuple[list[Sample], list[Sample], list[Sample]]:
    """(train, calib, holdout)：train 末尾 20% 已在调用方切早停验证。"""
    days = sorted({s.trade_date for s in samples})
    boundary = days[-HOLDOUT_DAYS] if len(days) > HOLDOUT_DAYS else days[0]
    dev = [s for s in samples if s.trade_date < boundary]
    holdout = [s for s in samples if s.trade_date >= boundary]
    calib = dev[-CALIB_WINDOW:]
    train = dev[:-CALIB_WINDOW] if len(dev) > CALIB_WINDOW else []
    return train, calib, holdout


def _train_valid_split(
    train: list[Sample], valid_frac: float = 0.2
) -> tuple[list[Sample], list[Sample]]:
    n_valid = max(40, int(len(train) * valid_frac))
    if len(train) <= n_valid + 40:
        return train, train[-max(20, len(train) // 5) :]
    return train[:-n_valid], train[-n_valid:]


async def train(
    session_factory: async_sessionmaker[AsyncSession],
    model_dir: Path,
    *,
    since: dt.date,
    until: dt.date,
    today: dt.date,
    force_activate: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    async with session_factory() as session:
        samples, ext_codes, sectors = await load_samples(session, since, until)
    if not samples:
        raise RuntimeError("无训练样本：请先回补 daily_bar / foreign_daily")

    feature_names = base_feature_names(ext_codes)
    by_code: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_code[s.variety_code].append(s)

    # 版本号：同日多次训练自动递增序号
    version = f"gbm-{today:%Y%m%d}-01"
    async with session_factory() as session:
        existing = (
            await session.execute(
                text("SELECT version FROM model_registry WHERE version LIKE :p"),
                {"p": f"gbm-{today:%Y%m%d}-%"},
            )
        ).scalars().all()
    if existing:
        version = f"gbm-{today:%Y%m%d}-{len(existing) + 1:02d}"

    # 第一遍：确定哪些品种样本不足需池化
    splits = {code: _split(items) for code, items in by_code.items()}
    pooled_codes = {
        code for code, (tr, _, _) in splits.items() if len(tr) < MIN_TRAIN_SAMPLES
    }
    pooled_names = pooled_feature_names(ext_codes, sorted(by_code))

    # 板块池化训练集：同板块全部品种（含足量品种，one-hot 区分）
    sector_train: dict[str, list[Sample]] = defaultdict(list)
    for code in pooled_codes:
        sector = sectors.get(code, "")
        for member, (tr, _, _) in splits.items():
            if sectors.get(member, "") == sector:
                sector_train[sector].extend(tr)

    models: dict[str, VarietyModel] = {}
    for code in sorted(by_code):
        train_s, calib_s, _ = splits[code]
        if len(calib_s) < MIN_CALIB_SAMPLES:
            logger.warning("calib_window_too_small code=%s n=%d → 纯规则", code, len(calib_s))
            continue
        pooled = code in pooled_codes
        names = pooled_names if pooled else feature_names
        if pooled:
            sector = sectors.get(code, "")
            pool = sector_train.get(sector, [])
            if len(pool) < MIN_TRAIN_SAMPLES // 2:
                logger.warning("sector_pool_too_small code=%s sector=%s → 纯规则", code, sector)
                continue
            fit_s, valid_s = _train_valid_split(pool)
        else:
            fit_s, valid_s = _train_valid_split(train_s)

        x_fit, y_fit = to_matrix(fit_s, names)
        x_valid, y_valid = to_matrix(valid_s, names)
        booster = train_booster(x_fit, y_fit, x_valid, y_valid, names)

        # 校准窗：GBM 概率 → isotonic；规则/GBM logloss → 融合权重
        x_calib, y_calib = to_matrix(calib_s, names)
        calibrator = IsotonicCalibrator().fit(predict_proba(booster, x_calib), y_calib)
        ll_gbm = logloss(calibrator.transform(predict_proba(booster, x_calib)), y_calib)
        ll_rule = logloss([list(s.rule_p) for s in calib_s], y_calib)
        w_rule, w_gbm = fusion_weights(ll_rule, ll_gbm)

        artifact = save_artifacts(model_dir, version, code, booster, calibrator)
        importance = {
            n: float(v)
            for n, v in zip(names, booster.feature_importance(importance_type="gain"), strict=True)
            if v > 0
        }
        models[code] = VarietyModel(
            code=code,
            artifact=artifact,
            pooled=pooled,
            w_rule=w_rule,
            w_gbm=w_gbm,
            importance=importance,
            booster=booster,
            calibrator=calibrator,
        )
        logger.info(
            "trained code=%s pooled=%s train=%d calib=%d ll_rule=%.4f ll_gbm=%.4f w=(%.2f,%.2f)",
            code, pooled, len(fit_s), len(calib_s), ll_rule, ll_gbm, w_rule, w_gbm,
        )

    # holdout 最终评估：融合 vs 规则基线（同一批样本）
    holdout_samples = [s for code in sorted(by_code) for s in splits[code][2]]
    fusion_probs: list[list[float]] = []
    fusion_dirs: list[int] = []
    fusion_confs: list[float] = []
    covered: list[Sample] = []
    for s in holdout_samples:
        m = models.get(s.variety_code)
        if m is None:
            continue
        names = pooled_names if m.pooled else feature_names
        x, _ = to_matrix([s], names)
        p_gbm = m.calibrator.transform(predict_proba(m.booster, x))[0]
        fused = fuse_probs(s.rule_p, p_gbm, m.w_rule, m.w_gbm)
        fusion_probs.append(fused)
        fusion_dirs.append(direction_from_probs(fused))
        fusion_confs.append(max(fused))
        covered.append(s)

    holdout_rule = summarize(_rule_records(covered))
    holdout_fusion = summarize(_records(covered, fusion_probs, fusion_dirs, fusion_confs))

    passed = (
        holdout_fusion["balanced_accuracy"]
        >= holdout_rule["balanced_accuracy"] + GATE_BALANCED_ACC_DELTA
        and holdout_fusion["brier_direction"] <= GATE_BRIER_DIRECTION
        and holdout_fusion["ece"] <= GATE_ECE
    )
    status = "active" if (passed or force_activate) else "shadow"

    params = {
        "ext_codes": ext_codes,
        "feature_names": feature_names,
        "pooled_feature_names": pooled_names,
        "models": {
            code: {"artifact": m.artifact, "pooled": m.pooled} for code, m in models.items()
        },
        "fusion": {
            code: {"w_rule": m.w_rule, "w_gbm": m.w_gbm} for code, m in models.items()
        },
        "importance": {code: m.importance for code, m in models.items()},
        "calib_window": CALIB_WINDOW,
        "min_train_samples": MIN_TRAIN_SAMPLES,
    }
    metrics = {
        "holdout_rule": holdout_rule,
        "holdout_fusion": holdout_fusion,
        "gate": {
            "balanced_acc_delta": round(
                holdout_fusion["balanced_accuracy"] - holdout_rule["balanced_accuracy"], 4
            ),
            "passed": passed,
        },
    }

    train_days = [s.trade_date for code_tr in splits.values() for s in code_tr[0]]
    if not dry_run:
        async with session_factory() as session, session.begin():
            if status == "active":
                await session.execute(
                    text(
                        "UPDATE model_registry SET status = 'retired'"
                        " WHERE kind IN ('fusion', 'gbm') AND status = 'active'"
                    )
                )
            await session.execute(
                text(
                    "INSERT INTO model_registry"
                    " (version, kind, params, train_start, train_end, metrics, status)"
                    " VALUES (:v, 'fusion', CAST(:p AS jsonb), :ts, :te, CAST(:m AS jsonb), :st)"
                ),
                {
                    "v": version,
                    "p": _json(params),
                    "ts": min(train_days) if train_days else None,
                    "te": max(train_days) if train_days else None,
                    "m": _json(metrics),
                    "st": status,
                },
            )

    return {
        "version": version,
        "status": status,
        "passed": passed,
        "models": {code: {"pooled": m.pooled, "w_rule": m.w_rule, "w_gbm": m.w_gbm}
                   for code, m in models.items()},
        "metrics": metrics,
        "n_holdout": len(covered),
    }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)
