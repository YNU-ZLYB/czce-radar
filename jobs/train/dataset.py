"""训练数据集构建：离线批量与线上单点共用同一向量化路径。

防泄漏铁律（与 scripts/backtest.py 同口径）：目标日 T 的样本，
价格/外盘/产业/资讯一律只用 trade_date < T 的数据；σ 用 T-1 及之前的 20 日收益。
标签按《02》§3.2 的 σ 自适应带三分类：0=偏空(含强空) 1=震荡 2=偏多(含强多)。

``display_only`` 字段（spark_6h / volume_ratio 等盘面动量）永不进入特征名表，
测试中有硬断言（ADR-5）。
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.predict.rules import bands, score

LABEL_DOWN, LABEL_FLAT, LABEL_UP = 0, 1, 2

#: 永不入模的 display_only 键（ADR-5，测试断言用）
DISPLAY_ONLY_KEYS = ("spark_6h", "volume_ratio")

_PRICE_FEATURES = ("ret_1d", "ret_5d", "vol_20d", "pos_20d")
_LINKAGE_FEATURES = ("trans_prior", "trans_q", "n_foreign_missing", "n_edges")
_INDUSTRY_FEATURES = ("receipt_chg_5d", "inventory_pct", "basis_pct")
_SEASON_FEATURES = ("season_bias", "season_conf", "month")
_NEWS_FEATURES = ("news_sentiment_lag1", "news_count_lag1")
_EVENT_FEATURES = ("is_event_day",)


def base_feature_names(ext_codes: Sequence[str]) -> list[str]:
    """固定顺序的基础特征名表（不含品种 one-hot）。"""
    names = list(_PRICE_FEATURES)
    for ext in sorted(ext_codes):
        names += [f"f_{ext}_chg_1d", f"f_{ext}_chg_3d"]
    names += list(_LINKAGE_FEATURES)
    names += list(_INDUSTRY_FEATURES)
    names += list(_SEASON_FEATURES)
    names += list(_NEWS_FEATURES)
    names += list(_EVENT_FEATURES)
    return names


def pooled_feature_names(ext_codes: Sequence[str], variety_codes: Sequence[str]) -> list[str]:
    """板块池化模型：基础特征 + 品种 one-hot（《02》§5.2）。"""
    return base_feature_names(ext_codes) + [f"var_{c}" for c in sorted(variety_codes)]


@dataclass(frozen=True, slots=True)
class Sample:
    variety_code: str
    trade_date: dt.date
    features: dict[str, float | None]
    label: int
    actual_change: float
    rule_dir: int  # 规则层 direction（与线上同一 score() 路径）
    rule_confidence: float
    rule_p: tuple[float, float, float]  # (p_down, p_flat, p_up)


@dataclass(frozen=True, slots=True)
class Edge:
    ext: str
    prior: float
    q: float


@dataclass(frozen=True, slots=True)
class Series:
    """industry 等单值序列（按日期升序）。"""

    dates: list[dt.date]
    values: list[float | None]


@dataclass(frozen=True, slots=True)
class ForeignSeries:
    """外盘日线：change_pct（%）与收盘价双序列（chg_3d 由收盘价算）。"""

    dates: list[dt.date]
    change_pct: list[float | None]
    closes: list[float]


@dataclass(frozen=True, slots=True)
class NewsSeries:
    dates: list[dt.date]
    scores: list[float | None]
    counts: list[float | None]


def _std(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _pct(new: float, old: float) -> float | None:
    return (new / old - 1) * 100 if old else None


def _idx_before(dates: list[dt.date], day: dt.date) -> int:
    """dates 中严格小于 day 的最后一个下标（无则 -1）。"""
    return bisect.bisect_left(dates, day) - 1


def feature_dict(
    *,
    price: dict[str, float | None],
    foreign: dict[str, dict[str, float | None]],
    edges: Sequence[Edge],
    industry: dict[str, float | None],
    season_bias: float | None,
    season_conf: float | None,
    month: int,
    news_sentiment_lag1: float | None,
    news_count_lag1: float | None,
    is_event_day: bool,
    ext_codes: Sequence[str],
) -> dict[str, float | None]:
    """把一份 f1 语义的特征原料摊平成固定键空间（线上线下唯一口径）。"""
    out: dict[str, float | None] = {k: price.get(k) for k in _PRICE_FEATURES}
    for ext in sorted(ext_codes):
        block = foreign.get(ext) or {}
        out[f"f_{ext}_chg_1d"] = block.get("chg_1d")
        out[f"f_{ext}_chg_3d"] = block.get("chg_3d")

    trans_prior = 0.0
    trans_q = 0.0
    n_missing = 0
    for e in edges:
        chg = (foreign.get(e.ext) or {}).get("chg_1d")
        if chg is None:
            n_missing += 1
            continue
        trans_prior += chg * e.prior
        trans_q += chg * e.prior * e.q
    n_edges = len(edges)
    out["trans_prior"] = round(trans_prior, 6) if n_edges else None
    out["trans_q"] = round(trans_q, 6) if n_edges else None
    out["n_foreign_missing"] = float(n_missing)
    out["n_edges"] = float(n_edges)

    for k in _INDUSTRY_FEATURES:
        out[k] = industry.get(k)
    out["season_bias"] = season_bias
    out["season_conf"] = season_conf
    out["month"] = float(month)
    out["news_sentiment_lag1"] = news_sentiment_lag1
    out["news_count_lag1"] = news_count_lag1
    out["is_event_day"] = 1.0 if is_event_day else 0.0
    return out


def to_matrix(
    samples: Sequence[Sample], names: Sequence[str]
) -> tuple[list[list[float]], list[int]]:
    """(X, y)；缺失 → NaN（LightGBM 原生处理）；var_* one-hot 在此展开。"""
    x: list[list[float]] = []
    y: list[int] = []
    for s in samples:
        row: list[float] = []
        for n in names:
            if n.startswith("var_"):
                row.append(1.0 if s.variety_code == n[4:] else 0.0)
                continue
            v = s.features.get(n)
            row.append(float("nan") if v is None else float(v))
        x.append(row)
        y.append(s.label)
    return x, y


def _industry_at(industry: dict[str, Series], day: dt.date) -> dict[str, float | None]:
    """trade_date < day 的产业指标（与 jobs/features/build.py 同口径）。"""
    out: dict[str, float | None] = {}
    receipt = industry.get("warehouse_receipt")
    if receipt is not None and (idx := _idx_before(receipt.dates, day)) >= 5:
        cur, prev5 = receipt.values[idx], receipt.values[idx - 5]
        out["receipt_chg_5d"] = _pct(cur, prev5) if cur is not None and prev5 is not None else None
    else:
        out["receipt_chg_5d"] = None

    inv = industry.get("inventory")
    out["inventory_pct"] = None
    if inv is not None and (inv_idx := _idx_before(inv.dates, day)) >= 0:
        cur = inv.values[inv_idx]
        if cur is not None:
            hist = [v for v in inv.values[max(0, inv_idx - 249) : inv_idx + 1] if v is not None]
            if hist:
                out["inventory_pct"] = round(
                    sum(1 for v in hist if v < cur) / len(hist) * 100, 1
                )

    basis = industry.get("basis")
    if basis is not None and (idx := _idx_before(basis.dates, day)) >= 0:
        out["basis_pct"] = basis.values[idx]
    else:
        out["basis_pct"] = None
    return out


def build_samples_for_variety(
    code: str,
    bars: list[tuple[dt.date, float]],
    edges: Sequence[Edge],
    foreign: dict[str, ForeignSeries],
    seasonality: dict[int, tuple[float | None, float | None]],
    industry: dict[str, Series],
    news: NewsSeries | None,
    ext_codes: Sequence[str],
    since: dt.date,
    until: dt.date,
) -> list[Sample]:
    """纯函数：单品种全历史 → 样本序列（与 scripts/backtest.py 同一窗口口径）。"""
    if len(bars) < 22:
        return []
    closes = [c for _, c in bars]
    returns: list[float | None] = [None]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        returns.append((closes[i] / prev - 1) if prev else None)

    samples: list[Sample] = []
    for i in range(21, len(bars)):
        day = bars[i][0]
        if day < since or day > until:
            continue
        window = returns[i - 20 : i]
        if any(v is None for v in window):
            continue
        sigma = _std([float(v) for v in window if v is not None])
        if sigma is None:
            continue
        sigma_pct = sigma * 100

        # price（全部 ≤ T-1）
        window20 = closes[i - 20 : i]
        lo, hi = min(window20), max(window20)
        price = {
            "ret_1d": _pct(closes[i - 1], closes[i - 2]),
            "ret_5d": _pct(closes[i - 1], closes[i - 6]) if i >= 6 else None,
            "vol_20d": round(sigma_pct, 4),
            "pos_20d": round((closes[i - 1] - lo) / (hi - lo), 3) if hi > lo else None,
        }

        # foreign（trade_date < T 的最近收盘）
        fblocks: dict[str, dict[str, float | None]] = {}
        for ext in ext_codes:
            series = foreign.get(ext)
            if series is None:
                continue
            idx = _idx_before(series.dates, day)
            if idx < 0:
                continue
            chg3 = _pct(series.closes[idx], series.closes[idx - 2]) if idx >= 2 else None
            fblocks[ext] = {"chg_1d": series.change_pct[idx], "chg_3d": chg3}

        bias, conf = seasonality.get(day.month, (None, None))
        n_senti: float | None = None
        n_count: float | None = None
        if news is not None and (idx := _idx_before(news.dates, day)) >= 0:
            n_senti = news.scores[idx]
            n_count = news.counts[idx]

        feats = feature_dict(
            price=price,
            foreign=fblocks,
            edges=edges,
            industry=_industry_at(industry, day),
            season_bias=bias,
            season_conf=conf,
            month=day.month,
            news_sentiment_lag1=n_senti,
            news_count_lag1=n_count,
            is_event_day=False,
            ext_codes=ext_codes,
        )

        # 标签：T 日实际涨跌 vs T-1 收盘，σ 自适应带
        actual = (closes[i] / closes[i - 1] - 1) * 100
        flat, _ = bands(sigma_pct)
        label = LABEL_UP if actual > flat else LABEL_DOWN if actual < -flat else LABEL_FLAT

        # 规则层基线输出（同一 score() 路径，供校准窗 logloss 对比与 holdout 基线）
        n_missing = sum(1 for e in edges if (fblocks.get(e.ext) or {}).get("chg_1d") is None)
        rule_out = score(
            {
                "price": {"vol_20d": round(sigma_pct, 4)},
                "linkage": [
                    {
                        "name": e.ext,
                        "ext": e.ext,
                        "prior": e.prior,
                        "q": e.q,
                        "chg_1d": (fblocks.get(e.ext) or {}).get("chg_1d"),
                    }
                    for e in edges
                    if (fblocks.get(e.ext) or {}).get("chg_1d") is not None
                ],
                "seasonality": {"month": day.month, "bias": bias, "confidence": conf},
                "news": {"sentiment_lag1": n_senti},
                "event": {"is_event_day": False},
            },
            missing_foreign=n_missing,
        )
        samples.append(
            Sample(
                variety_code=code,
                trade_date=day,
                features=feats,
                label=label,
                actual_change=round(actual, 4),
                rule_dir=rule_out.direction,
                rule_confidence=rule_out.confidence,
                rule_p=(rule_out.p_down, rule_out.p_flat, rule_out.p_up),
            )
        )
    return samples


async def _load_edges(session: AsyncSession) -> dict[str, list[Edge]]:
    rows = (
        await session.execute(
            text(
                "SELECT e.variety_code, e.proxy_ext, e.prior, v.linkage_q"
                " FROM knowledge_edge e JOIN variety v ON v.code = e.variety_code"
                " WHERE e.proxy_ext IS NOT NULL"
            )
        )
    ).all()
    grouped: dict[str, list[Edge]] = defaultdict(list)
    for row in rows:
        grouped[row.variety_code].append(
            Edge(ext=row.proxy_ext, prior=float(row.prior), q=float(row.linkage_q))
        )
    return grouped


async def load_samples(
    session: AsyncSession, since: dt.date, until: dt.date
) -> tuple[list[Sample], list[str], dict[str, str]]:
    """批量构建全品种样本；返回 (samples, ext_codes, variety→sector)。"""
    edges_map = await _load_edges(session)
    ext_codes = sorted({e.ext for items in edges_map.values() for e in items})

    sector_rows = (await session.execute(text("SELECT code, sector FROM variety"))).all()
    sectors = {r.code: r.sector for r in sector_rows}

    season_rows = (
        await session.execute(
            text("SELECT variety_code, month, bias, confidence FROM seasonality")
        )
    ).all()
    seasonality: dict[str, dict[int, tuple[float | None, float | None]]] = defaultdict(dict)
    for r in season_rows:
        seasonality[r.variety_code][int(r.month)] = (
            float(r.bias) if r.bias is not None else None,
            float(r.confidence) if r.confidence is not None else None,
        )

    bar_rows = (
        await session.execute(
            text(
                "SELECT variety_code, trade_date, close FROM daily_bar"
                " WHERE trade_date >= :a AND trade_date <= :b"
                " ORDER BY variety_code, trade_date"
            ),
            {"a": since - dt.timedelta(days=120), "b": until},
        )
    ).all()
    bars_map: dict[str, list[tuple[dt.date, float]]] = defaultdict(list)
    for r in bar_rows:
        bars_map[r.variety_code].append((r.trade_date, float(r.close)))

    foreign: dict[str, ForeignSeries] = {}
    if ext_codes:
        frows = (
            await session.execute(
                text(
                    "SELECT ext_code, trade_date, change_pct, close FROM foreign_daily"
                    " WHERE ext_code = ANY(:codes) AND trade_date >= :a AND trade_date <= :b"
                    " ORDER BY ext_code, trade_date"
                ),
                {"codes": ext_codes, "a": since - dt.timedelta(days=40), "b": until},
            )
        ).all()
        grouped: dict[str, list[tuple[dt.date, float | None, float]]] = defaultdict(list)
        for r in frows:
            grouped[r.ext_code].append(
                (
                    r.trade_date,
                    float(r.change_pct) if r.change_pct is not None else None,
                    float(r.close),
                )
            )
        foreign = {
            ext: ForeignSeries(
                dates=[d for d, _, _ in items],
                change_pct=[v for _, v, _ in items],
                closes=[c for _, _, c in items],
            )
            for ext, items in grouped.items()
        }

    ind_rows = (
        await session.execute(
            text(
                "SELECT variety_code, kind, trade_date, value FROM industry_metric"
                " WHERE trade_date >= :a AND trade_date <= :b"
                " ORDER BY variety_code, kind, trade_date"
            ),
            {"a": since - dt.timedelta(days=400), "b": until},
        )
    ).all()
    industry_grouped: dict[str, dict[str, list[tuple[dt.date, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in ind_rows:
        industry_grouped[r.variety_code][r.kind].append((r.trade_date, float(r.value)))
    industry_map = {
        code: {
            kind: Series(dates=[d for d, _ in items], values=[v for _, v in items])
            for kind, items in kinds.items()
        }
        for code, kinds in industry_grouped.items()
    }

    news_rows = (
        await session.execute(
            text(
                "SELECT variety_code, trade_date, score, doc_count FROM news_sentiment_d"
                " WHERE trade_date >= :a AND trade_date <= :b ORDER BY variety_code, trade_date"
            ),
            {"a": since - dt.timedelta(days=40), "b": until},
        )
    ).all()
    news_grouped: dict[str, list[tuple[dt.date, float | None, float | None]]] = defaultdict(list)
    for r in news_rows:
        news_grouped[r.variety_code].append(
            (
                r.trade_date,
                float(r.score) if r.score is not None else None,
                float(r.doc_count) if r.doc_count is not None else None,
            )
        )
    news_map = {
        code: NewsSeries(
            dates=[d for d, _, _ in items],
            scores=[s for _, s, _ in items],
            counts=[c for _, _, c in items],
        )
        for code, items in news_grouped.items()
    }

    samples: list[Sample] = []
    for code in sorted(bars_map):
        samples.extend(
            build_samples_for_variety(
                code,
                bars_map[code],
                edges_map.get(code, []),
                foreign,
                seasonality.get(code, {}),
                industry_map.get(code, {}),
                news_map.get(code),
                ext_codes,
                since,
                until,
            )
        )
    return samples, ext_codes, sectors
