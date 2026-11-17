"""离线实验：置信度阈值 vs Meta-Labeling vs 状态条件化阈值。

问题：现有规则只在 18% 的日子给方向，命中 65.8%。降门槛换覆盖会混入假阳性。
本脚本检验三件事是否有数据支撑：

1. 主模型放开到每天开枪（argmax(p_up,p_down)），再用置信度筛回 top-k，
   能否达到或超过原规则的命中率；
2. 用独立元模型（判别"这次信号会不会对"，特征与方向因子不同）筛 top-k，
   是否优于直接用置信度筛；
3. 按市场波动状态分桶后，"置信度→命中率"曲线是否显著分化（若是，
   全局单一阈值就是错的口径）。

数据与线上同一条路径：复用 ``scripts.backtest`` 的加载器与 ``_build_records``，
q 取时点估计（无未来信息）。

用法（容器内）：
    python -m scripts.meta_label_experiment
    python -m scripts.meta_label_experiment --since 2022-11-10 --holdout-days 60
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from jobs.predict.metrics import BacktestRecord
from jobs.predict.rules import band_scale_for
from scripts.backtest import (
    _build_records,
    _load_daily,
    _load_edges,
    _load_foreign,
    _load_seasonality,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
FOLDS_START = 250  # walk-forward 最少训练交易日
BLOCK = 21  # 每折测试交易日数
KEEP = (0.05, 0.10, 0.15, 0.20, 0.30, 0.40)
FEATS = [
    "confidence",
    "dir_edge",
    "max_abs_foreign",
    "agreement",
    "vol20",
    "er20",
    "hit60",
    "open60",
    "dow",
]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


@dataclass(slots=True)
class Panel:
    """按 (品种, 日期) 排序后的扁平数组。"""

    code: np.ndarray
    day: np.ndarray
    pred_dir: np.ndarray
    conf: np.ndarray
    p_up: np.ndarray
    p_down: np.ndarray
    max_abs_foreign: np.ndarray
    trans_sign: np.ndarray
    actual_change: np.ndarray

    def __len__(self) -> int:
        return len(self.code)


def _panel(records: list[BacktestRecord]) -> Panel:
    ordered = sorted(records, key=lambda r: (r.variety_code, r.trade_date))
    return Panel(
        code=np.array([r.variety_code for r in ordered]),
        day=np.array([r.trade_date.toordinal() for r in ordered]),
        pred_dir=np.array([r.pred_dir for r in ordered], dtype=int),
        conf=np.array([r.confidence for r in ordered], dtype=float),
        p_up=np.array([r.p_up for r in ordered], dtype=float),
        p_down=np.array([r.p_down for r in ordered], dtype=float),
        max_abs_foreign=np.array([r.max_abs_foreign for r in ordered], dtype=float),
        trans_sign=np.array([r.transmission_sign for r in ordered], dtype=int),
        actual_change=np.array([r.actual_change for r in ordered], dtype=float),
    )


def _features(p: Panel) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """返回 (X, y, mask)。所有行情类特征严格滞后一日。"""
    n = len(p)
    vol20 = np.full(n, np.nan)
    er20 = np.full(n, np.nan)
    hit60 = np.full(n, np.nan)
    open60 = np.full(n, np.nan)
    prim = np.where(p.p_up > p.p_down, 1, -1)

    start = 0
    for i in range(1, n + 1):
        if i < n and p.code[i] == p.code[i - 1]:
            continue
        # 品种切片 [start, i)
        chg = p.actual_change[start:i] / 100.0
        m = i - start
        idx = 100.0 * np.cumprod(1.0 + chg) if m else np.array([])
        sign_ok = (prim[start:i] == np.sign(chg)).astype(float)
        for j in range(m):
            if j >= 20:
                w = chg[j - 20 : j]
                vol20[start + j] = float(np.std(w, ddof=1) * 100)
                path = float(np.abs(w).sum())
                ret = float(idx[j - 1] / idx[j - 21] - 1.0) if j >= 21 else np.nan
                er20[start + j] = abs(ret) / path if path > 0 else np.nan
            if j >= 60:
                hit60[start + j] = float(sign_ok[j - 60 : j].mean())
                open60[start + j] = float(
                    (p.pred_dir[start:i][j - 60 : j] != 0).mean()
                )
        start = i

    x = np.column_stack(
        [
            p.conf,
            np.abs(p.p_up - p.p_down),
            p.max_abs_foreign,
            (p.trans_sign == prim).astype(float),
            vol20,
            er20,
            hit60,
            open60,
            np.array([dt.date.fromordinal(int(d)).weekday() for d in p.day], dtype=float),
        ]
    )
    y = (prim == np.sign(p.actual_change)).astype(int)
    mask = p.actual_change != 0
    return x, y, mask, prim


def _impute(tr: np.ndarray, te: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    med = np.nanmedian(tr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    return np.where(np.isnan(tr), med, tr), np.where(np.isnan(te), med, te)


def _logit(tr_x: np.ndarray, tr_y: np.ndarray, te_x: np.ndarray) -> np.ndarray:
    tr_x, te_x = _impute(tr_x, te_x)
    sc = StandardScaler().fit(tr_x)
    m = LogisticRegression(max_iter=500, C=0.5)
    m.fit(sc.transform(tr_x), tr_y)
    return m.predict_proba(sc.transform(te_x))[:, 1]


def _hgb(
    tr_x: np.ndarray, tr_y: np.ndarray, te_x: np.ndarray, tr_code: np.ndarray, te_code: np.ndarray
) -> np.ndarray:
    cats = np.array(sorted(set(tr_code.tolist())))
    tr_x, te_x = _impute(tr_x, te_x)
    c_tr = np.searchsorted(cats, tr_code)
    c_te = np.searchsorted(cats, te_code)
    xtr = np.column_stack([tr_x, c_tr])
    xte = np.column_stack([te_x, c_te])
    m = HistGradientBoostingClassifier(
        max_depth=3,
        max_iter=200,
        learning_rate=0.05,
        min_samples_leaf=40,
        l2_regularization=1.0,
        categorical_features=[xtr.shape[1] - 1],
        random_state=0,
    )
    m.fit(xtr, tr_y)
    return m.predict_proba(xte)[:, 1]


def _frontier(score: np.ndarray, y: np.ndarray, name: str) -> dict[float, dict]:
    order = np.argsort(-score)
    out: dict[float, dict] = {}
    n = len(order)
    for f in KEEP:
        k = max(1, int(n * f))
        pick = order[:k]
        hits = int(y[pick].sum())
        lo, hi = wilson(hits, k)
        out[f] = {"model": name, "n": k, "acc": hits / k, "lo": lo, "hi": hi}
    return out


def _fmt_table(rows: dict[str, dict[float, dict]]) -> str:
    head = "| 保留比例 | " + " | ".join(f"{k:.0%}" for k in KEEP) + " |"
    sep = "|---" * (len(KEEP) + 1) + "|"
    lines = [
        head,
        sep,
    ]
    for name, per in rows.items():
        cells = " | ".join(f"{per[f]['acc'] * 100:.1f}" for f in KEEP)
        lines.append(f"| {name} | {cells} |")
    return "\n".join(lines)


async def _load_records(since: dt.date, until: dt.date) -> list[BacktestRecord]:
    settings = load_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            edges = await _load_edges(session)
            season = await _load_seasonality(session)
            daily = await _load_daily(session, since, until)
            ext_codes = sorted({e.ext for items in edges.values() for e in items})
            foreign = await _load_foreign(session, ext_codes, since, until)
        out: list[BacktestRecord] = []
        for code in sorted(daily):
            out.extend(
                _build_records(
                    code,
                    daily[code],
                    edges.get(code, []),
                    foreign,
                    season,
                    since,
                    until,
                    "pit",
                    band_scale=band_scale_for(code),
                )
            )
        return out
    finally:
        await engine.dispose()


def _run(p: Panel, x: np.ndarray, y: np.ndarray, prim: np.ndarray, holdout_days: int) -> str:
    days = np.array(sorted(set(p.day.tolist())))
    boundary = days[-holdout_days]
    dev = p.day < boundary
    hold = ~dev

    def split(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return x[mask], y[mask], p.day[mask]

    xd, yd, dd = split(dev)
    xh, yh, dh = split(hold)
    cd, ch = p.code[dev], p.code[hold]

    lines: list[str] = []

    # 原始规则（只在开口日给方向）
    for label, m in (("dev", dev), ("holdout", hold)):
        opened = m & (p.pred_dir != 0)
        ok = (p.pred_dir[opened] == np.sign(p.actual_change[opened])).astype(int)
        lo, hi = wilson(int(ok.sum()), len(ok))
        lines.append(
            f"- 规则原样 {label}: 覆盖 {opened.sum() / m.sum():.2%} "
            f"n={len(ok)} 命中 {ok.mean():.3f} CI[{lo:.3f},{hi:.3f}]"
        )

    # 主模型全开口
    full_acc = yd.mean()
    lines.append(f"- 主模型全开口(100% 覆盖) dev 命中 {full_acc:.3f}")

    # dev walk-forward OOF
    oof_logit = np.full(len(yd), np.nan)
    oof_hgb = np.full(len(yd), np.nan)
    for s in range(FOLDS_START, len(days), BLOCK):
        tr_m = dd < days[s - 1]
        te_m = (dd >= days[s]) & (dd < (days[s + BLOCK] if s + BLOCK < len(days) else np.inf))
        if te_m.sum() == 0 or tr_m.sum() < 500:
            continue
        oof_logit[te_m] = _logit(xd[tr_m], yd[tr_m], xd[te_m])
        oof_hgb[te_m] = _hgb(xd[tr_m], yd[tr_m], xd[te_m], cd[tr_m], cd[te_m])

    ok = ~np.isnan(oof_hgb)
    dev_rows = {
        "conf(现状)": _frontier(p.conf[dev][ok], yd[ok], "conf"),
        "meta-logit": _frontier(oof_logit[ok], yd[ok], "meta-logit"),
        "meta-hgb": _frontier(oof_hgb[ok], yd[ok], "meta-hgb"),
    }
    lines += ["", "## dev（walk-forward OOF）命中率 %", _fmt_table(dev_rows)]

    # holdout：dev 全量训练一次
    if hold.sum() > 0:
        hl = _logit(xd, yd, xh)
        hh = _hgb(xd, yd, xh, cd, ch)
        hold_rows = {
            "conf(现状)": _frontier(p.conf[hold], yh, "conf"),
            "meta-logit": _frontier(hl, yh, "meta-logit"),
            "meta-hgb": _frontier(hh, yh, "meta-hgb"),
        }
        lines += ["", f"## holdout（n={int(hold.sum())}）命中率 %", _fmt_table(hold_rows)]
        k20 = int(hold.sum() * 0.2)
        lines.append(
            f"- holdout 20% 保留时样本数 n={k20}，95%CI 宽度约 ±"
            f"{(hold_rows['meta-hgb'][0.2]['hi'] - hold_rows['meta-hgb'][0.2]['lo']) * 50:.1f}pp"
        )

    # 状态条件化：市场波动四分位内的命中-置信度关系
    mkt: dict[int, list[float]] = {}
    for i in range(len(p)):
        if not math.isnan(x[i, 4]):
            mkt.setdefault(int(p.day[i]), []).append(float(x[i, 4]))
    med = {d: float(np.median(v)) for d, v in mkt.items()}
    mv = np.array([med.get(int(d), np.nan) for d in p.day])
    qs = np.nanquantile(mv[dev], [0.25, 0.5, 0.75])
    lines += ["", "## 状态条件化（dev，按当日全市场 vol20 四分位）"]
    for r in range(4):
        m = dev & (np.digitize(mv, qs) == r)
        cells = []
        for lo_q, hi_q in ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)):
            sel = m & (p.conf >= lo_q) & (p.conf < hi_q)
            if sel.sum() >= 50:
                cells.append(f"conf{lo_q:.1f}-{hi_q:.1f}: n={int(sel.sum())} {y[sel].mean():.1%}")
        lines.append(f"- Q{r + 1} n={int(m.sum())}: " + " | ".join(cells) if cells else f"- Q{r + 1} n={int(m.sum())}")

    # 20% 覆盖率下的配对 bootstrap
    lines += ["", "## 20% 覆盖率：meta 相对 conf 的差值（dev OOF bootstrap）"]
    for name, sc in (("meta-logit", oof_logit), ("meta-hgb", oof_hgb)):
        valid = ok & ~np.isnan(sc)
        base = p.conf[dev][valid]
        alt = sc[valid]
        yy = yd[valid]
        k = int(len(yy) * 0.2)
        idx_c = np.argsort(-base)[:k]
        idx_a = np.argsort(-alt)[:k]
        rng = np.random.default_rng(0)
        diffs = np.empty(800)
        for b in range(800):
            pick = rng.integers(0, len(yy), len(yy))
            cc = yy[pick[np.argsort(-base[pick])[:k]]].mean()
            aa = yy[pick[np.argsort(-alt[pick])[:k]]].mean()
            diffs[b] = aa - cc
        lines.append(
            f"- {name} - conf: {diffs.mean() * 100:+.1f}pp "
            f"95%CI[{np.percentile(diffs, 2.5) * 100:+.1f},{np.percentile(diffs, 97.5) * 100:+.1f}] "
            f"选中重叠 {len(np.intersect1d(idx_c, idx_a))}/{k}"
        )
    return "\n".join(lines)


async def run(since: dt.date, until: dt.date, holdout_days: int) -> None:
    records = await _load_records(since, until)
    if not records:
        raise SystemExit("无样本：请先回补 daily_bar / foreign_daily")
    p = _panel(records)
    x, y, mask, prim = _features(p)
    p = Panel(
        code=p.code[mask],
        day=p.day[mask],
        pred_dir=p.pred_dir[mask],
        conf=p.conf[mask],
        p_up=p.p_up[mask],
        p_down=p.p_down[mask],
        max_abs_foreign=p.max_abs_foreign[mask],
        trans_sign=p.trans_sign[mask],
        actual_change=p.actual_change[mask],
    )
    x, y = x[mask], y[mask]
    body = _run(p, x, y, prim[mask], holdout_days)

    days = sorted(set(p.day.tolist()))
    head = (
        f"# Meta-Labeling 实验 · {dt.date.today():%Y-%m-%d}\n\n"
        f"- 样本 n={len(p)}，品种 {len(set(p.code.tolist()))}，"
        f"交易日 {len(days)}（{dt.date.fromordinal(days[0])} ~ {dt.date.fromordinal(days[-1])}）\n"
        f"- dev/holdout 切分：最近 {holdout_days} 个交易日为 holdout\n"
        "- 主模型 = argmax(p_up, p_down)（忽略震荡，每天开枪）；元模型特征不含方向因子\n"
    )
    report = head + body + "\n"
    print(report)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"meta_label_{dt.date.today():%Y%m%d}.md"
    path.write_text(report, encoding="utf-8")
    print(f"report: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meta-Labeling / 状态条件化阈值实验")
    today = dt.date.today()
    parser.add_argument("--since", type=dt.date.fromisoformat, default=today.replace(year=today.year - 4))
    parser.add_argument("--until", type=dt.date.fromisoformat, default=today)
    parser.add_argument("--holdout-days", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(run(args.since, args.until, args.holdout_days))


if __name__ == "__main__":
    main()
