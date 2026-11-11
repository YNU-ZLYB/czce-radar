"""Layer A 规则引擎（纯函数，无 I/O）。

公式（《02-数据与预测引擎》§5.1）：

    raw     = Σ(change × prior × q) + seasonal_bias + news_sentiment × 0.2
    signal  = clamp(raw × 22 / max(1, Σ prior×q), -100, 100)

三分类与幅度分档（§3.2，按品种波动率自适应，σ 单位为 %）：

    震荡带 = max(0.5, 0.35σ)
    强档带 = max(2.0, 1.0σ)

``exp_change`` 取 raw（期望涨跌幅 %），``bucket`` 由分档带判定，``direction``
由 bucket 推出（保证展示与方向永远一致）。概率用 Normal(exp_change, σ) 对
±震荡带积分并对三档归一化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SIGNAL_SCALE = 22.0
NEWS_WEIGHT = 0.2
DEFAULT_VOL = 1.5  # σ 缺失时的兜底（%）；不伪装成 0
MIN_EFFECTIVE_WEIGHT = 1.0

BUCKET_STRONG_UP = "强多"
BUCKET_UP = "偏多"
BUCKET_FLAT = "震荡"
BUCKET_DOWN = "偏空"
BUCKET_STRONG_DOWN = "强空"

_DIRECTION = {
    BUCKET_STRONG_UP: 1,
    BUCKET_UP: 1,
    BUCKET_FLAT: 0,
    BUCKET_DOWN: -1,
    BUCKET_STRONG_DOWN: -1,
}


@dataclass(frozen=True, slots=True)
class RuleOutput:
    direction: int
    bucket: str
    p_up: float
    p_flat: float
    p_down: float
    exp_change: float
    signal: int
    confidence: float
    contributor: list[dict[str, Any]]
    narrative: str


def _sigma(vol: float | None) -> float:
    return DEFAULT_VOL if vol is None or vol <= 0 else float(vol)


def bands(vol: float | None) -> tuple[float, float]:
    """返回 (震荡带, 强档带)，单位 %。"""
    sigma = _sigma(vol)
    return max(0.5, 0.35 * sigma), max(2.0, 1.0 * sigma)


def classify_bucket(exp_change: float, vol: float | None) -> str:
    flat, strong = bands(vol)
    if exp_change > strong:
        return BUCKET_STRONG_UP
    if exp_change > flat:
        return BUCKET_UP
    if exp_change < -strong:
        return BUCKET_STRONG_DOWN
    if exp_change < -flat:
        return BUCKET_DOWN
    return BUCKET_FLAT


def direction_of(bucket: str) -> int:
    return _DIRECTION[bucket]


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def probabilities(exp_change: float, vol: float | None) -> tuple[float, float, float]:
    """Normal(exp_change, σ) 对三档积分；返回 (p_up, p_flat, p_down)，和恒为 1。"""
    sigma = _sigma(vol)
    flat, _ = bands(vol)
    p_down = _norm_cdf((-flat - exp_change) / sigma)
    p_up = 1.0 - _norm_cdf((flat - exp_change) / sigma)
    p_down = min(max(p_down, 0.0), 1.0)
    p_up = min(max(p_up, 0.0), 1.0)
    p_up = round(p_up, 4)
    p_down = round(p_down, 4)
    p_flat = round(1.0 - p_up - p_down, 4)
    if p_flat < 0:  # 四舍五入极端情况下不产生负概率
        p_flat = 0.0
    return p_up, p_flat, p_down


def _contributor_rows(features: dict[str, Any]) -> tuple[list[dict[str, Any]], float, float]:
    """返回 (贡献者明细, raw, 有效权重和)。q=0 的传导项不展示也不计分。"""
    rows: list[dict[str, Any]] = []
    raw = 0.0
    effective = 0.0

    for item in features.get("linkage") or []:
        prior = item.get("prior")
        q = item.get("q")
        chg = item.get("chg_1d")
        if prior is None or q is None or chg is None:
            continue
        prior_f, q_f, chg_f = float(prior), float(q), float(chg)
        if q_f <= 0:
            continue  # 未校准/被证伪：不展示传导行
        contribution = chg_f * prior_f * q_f
        raw += contribution
        effective += prior_f * q_f
        rows.append(
            {
                "name": str(item.get("name") or item.get("ext") or "外盘"),
                "value": round(chg_f, 4),
                "weight": round(prior_f, 3),
                "q": round(q_f, 3),
                "contribution": round(contribution, 2),
            }
        )

    season = features.get("seasonality") or {}
    bias = season.get("bias")
    if bias is not None:
        # 季节性 bias 的单位是「信号分」，先按 SIGNAL_SCALE 折算成期望涨跌幅（%），
        # 否则一个月度量级的先验会压倒日度外盘传导。
        bias_f = float(bias) / SIGNAL_SCALE
        raw += bias_f
        month = season.get("month")
        stage = season.get("stage") or ""
        label = f"季节性·{month}月{stage}" if month else "季节性"
        rows.append(
            {
                "name": label,
                "value": None,
                "weight": 1.0,
                "q": round(float(season.get("confidence") or 0), 2),
                "contribution": round(bias_f, 2),
            }
        )

    news = features.get("news") or {}
    sentiment = news.get("sentiment_lag1")
    if sentiment is not None:
        sentiment_f = float(sentiment)
        contribution = sentiment_f * NEWS_WEIGHT
        raw += contribution
        rows.append(
            {
                "name": "资讯情绪(T-1)",
                "value": round(sentiment_f, 3),
                "weight": NEWS_WEIGHT,
                "q": 1.0,
                "contribution": round(contribution, 2),
            }
        )

    return rows, raw, effective


def _narrative(bucket: str, signal: int, rows: list[dict[str, Any]]) -> str:
    drivers = [r for r in rows if r["value"] is not None]
    drivers.sort(key=lambda r: abs(float(r["contribution"])), reverse=True)
    parts = [f"{r['name']} {float(r['value']):+.2f}%" for r in drivers[:2]]
    season = next((r for r in rows if r["value"] is None), None)
    if season is not None:
        parts.append(f"{season['name']} {float(season['contribution']):+.2f}")
    if not parts:
        return "无可用外盘与季节性信号，按震荡处理。"
    return "；".join(parts) + f"。综合判定{bucket}（signal {signal:+d}）。"


def score(
    features: dict[str, Any],
    *,
    missing_foreign: int = 0,
    is_event_day: bool | None = None,
) -> RuleOutput:
    """对一份 FeatureSnapshot.features 打分（确定性，无副作用）。"""
    rows, raw, effective = _contributor_rows(features)
    vol = (features.get("price") or {}).get("vol_20d")

    exp_change = round(raw, 4)
    bucket = classify_bucket(exp_change, vol)
    direction = direction_of(bucket)
    signal = round(raw * SIGNAL_SCALE / max(MIN_EFFECTIVE_WEIGHT, effective))
    signal = min(max(signal, -100), 100)

    p_up, p_flat, p_down = probabilities(exp_change, vol)
    confidence = max(p_up, p_flat, p_down)
    event_flag = (
        is_event_day
        if is_event_day is not None
        else bool((features.get("event") or {}).get("is_event_day"))
    )
    if missing_foreign > 2:
        confidence *= 0.7
    if event_flag:
        confidence *= 0.8

    return RuleOutput(
        direction=direction,
        bucket=bucket,
        p_up=p_up,
        p_flat=p_flat,
        p_down=p_down,
        exp_change=exp_change,
        signal=signal,
        confidence=round(confidence, 4),
        contributor=rows,
        narrative=_narrative(bucket, signal, rows),
    )
