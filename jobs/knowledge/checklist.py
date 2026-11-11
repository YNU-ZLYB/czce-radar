"""用户核实清单生成：知识库 YAML → Markdown（供逐条勾选确认）。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jobs.knowledge.models import KnowledgeDoc

_KIND_ZH = {
    "cost": "成本",
    "demand": "需求",
    "substitute": "替代",
    "policy": "政策",
    "weather": "天气",
}

_PROXY_ZH = {
    "OIL": "布伦特原油",
    "CT": "ICE 美棉",
    "S": "CBOT 大豆",
    "SM": "CBOT 豆粕",
    "BO": "CBOT 豆油",
    "C": "CBOT 玉米",
    "W": "CBOT 小麦",
    "NG": "NYMEX 天然气",
    "HG": "COMEX 铜",
    "DX": "美元指数",
}


def _proxy_label(driver_proxy_ext: str | None, driver_proxy_variety: str | None) -> str:
    parts: list[str] = []
    if driver_proxy_ext:
        parts.append(f"{_PROXY_ZH.get(driver_proxy_ext, driver_proxy_ext)}({driver_proxy_ext})")
    if driver_proxy_variety:
        parts.append(f"品种 {driver_proxy_variety}")
    return " / ".join(parts) if parts else "—"


def _mark(reviewed: bool) -> str:
    return "☑" if reviewed else "☐"


def _variety_section(doc: KnowledgeDoc) -> str:
    chain = " → ".join(
        "·".join(doc.chain.get(k, []))
        for k in ("upstream", "midstream", "downstream")
        if doc.chain.get(k)
    )
    status = f"已核实 by {doc.reviewed_by or '—'}" if doc.reviewed else "待核实"
    lines = [
        f"### {doc.name} {doc.code}（传导校准 q={doc.linkage_q:g}，{status}）",
        "",
        f"产业链：{chain}",
        "",
        "| # | 驱动 | 类型 | 代理 | 权重 | 滞后(日) | 机理 | 确认 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, d in enumerate(doc.drivers, 1):
        lines.append(
            f"| {i} | {d.name} | {_KIND_ZH.get(d.kind.value, d.kind.value)}"
            f" | {_proxy_label(d.proxy_ext, d.proxy_variety)} | {d.prior:g}"
            f" | {d.lag_days} | {d.mechanism} | {_mark(doc.reviewed)} |"
        )
    if doc.seasonality:
        lines += [
            "",
            "| 季节性 | 月份 | 偏移 | 把握 | 备注 | 确认 |",
            "|---|---|---|---|---|---|",
        ]
        for s in doc.seasonality:
            months = "/".join(f"{m}月" for m in s.months)
            lines.append(
                f"| {s.stage} | {months} | {s.bias:+g} | {s.confidence:g}"
                f" | {s.note or '—'} | {_mark(doc.reviewed)} |"
            )
    if doc.events:
        lines += ["", "关键事件：" + "；".join(doc.events)]
    if doc.risk_notes:
        lines += ["", "风险提示：" + "；".join(doc.risk_notes)]
    return "\n".join(lines)


def render_checklist(docs: Sequence[KnowledgeDoc]) -> str:
    """渲染整份核对清单 Markdown。"""
    total_drivers = sum(len(d.drivers) for d in docs)
    reviewed_docs = sum(1 for d in docs if d.reviewed)
    reviewers = sorted({d.reviewed_by for d in docs if d.reviewed and d.reviewed_by})
    if reviewed_docs == len(docs):
        by = "/".join(reviewers) or "—"
        status_line = f"> 状态：**已全部核实**（{len(docs)}/{len(docs)}，by {by}），导入器已生效。"
    else:
        status_line = (
            f"> 状态：已核实 {reviewed_docs}/{len(docs)}；逐条核对后把对应 YAML 的"
            " `reviewed: false` 改为 `true`，再运行导入器即可生效。"
        )
    header = [
        "# 知识库核实清单",
        "",
        status_line,
        "> `reviewed: true` 的知识才允许参与模型特征权重（M3 引擎强制校验）。",
        "",
        f"共 **{len(docs)}** 个品种、**{total_drivers}** 条驱动、"
        f"**{sum(len(d.seasonality) for d in docs)}** 条季节性规律。",
        "",
        "目录：" + "、".join(f"{d.code}" for d in docs),
        "",
        "---",
        "",
    ]
    body = "\n\n".join(_variety_section(d) for d in sorted(docs, key=lambda x: x.code))
    return "\n".join(header) + body + "\n"


def write_checklist(docs: Sequence[KnowledgeDoc], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_checklist(docs), encoding="utf-8")
    return out
