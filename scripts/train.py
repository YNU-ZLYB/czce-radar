"""一键训练（M4）：LightGBM + isotonic 校准 + 融合权重 → model_registry。

用法：
    python -m scripts.train                    # 最近 4 年，达标自动 active，否则 shadow
    python -m scripts.train --dry-run          # 只评估不写库
    python -m scripts.train --activate         # 未达标也强制上线（谨慎）

达标口径（DoD）：holdout Balanced Acc ≥ 规则基线+0.03 且 brier_direction ≤ 0.23 且 ECE ≤ 0.05。
产物落在 ``model_dir``（默认 models/，gitignored），registry 只存路径与指标。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from pathlib import Path

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from jobs.train.pipeline import train

ROOT = Path(__file__).resolve().parents[1]


async def _run(args: argparse.Namespace) -> dict:
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    model_dir = Path(settings.model_dir)
    if not model_dir.is_absolute():
        model_dir = ROOT / model_dir
    try:
        return await train(
            session_factory,
            model_dir,
            since=args.since,
            until=args.until,
            today=dt.date.today(),
            force_activate=args.activate,
            dry_run=args.dry_run,
        )
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="M4 融合模型训练")
    today = dt.date.today()
    parser.add_argument(
        "--since", type=dt.date.fromisoformat, default=today.replace(year=today.year - 4)
    )
    parser.add_argument("--until", type=dt.date.fromisoformat, default=today)
    parser.add_argument("--dry-run", action="store_true", help="只评估，不写 model_registry")
    parser.add_argument("--activate", action="store_true", help="未达门槛也强制 active")
    args = parser.parse_args()

    result = asyncio.run(_run(args))
    gate = result["metrics"]["gate"]
    rule = result["metrics"]["holdout_rule"]
    fusion = result["metrics"]["holdout_fusion"]
    print(f"\n=== 训练完成 version={result['version']} status={result['status']} ===")
    print(f"holdout 样本：{result['n_holdout']}")
    print(
        f"规则基线：BalancedAcc {rule['balanced_accuracy']:.3f} "
        f"Brier(方向) {rule['brier_direction']:.4f} ECE {rule['ece']:.3f}"
    )
    acc, delta = fusion["balanced_accuracy"], gate["balanced_acc_delta"]
    print(f"融合模型：BalancedAcc {acc:.3f}（Δ{delta:+.3f}）"
          f" Brier(方向) {fusion['brier_direction']:.4f} ECE {fusion['ece']:.3f}")
    print(f"门槛判定：{'✅ 通过 → active' if result['passed'] else '❌ 未过 → shadow'}")
    print(json.dumps(result["models"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
