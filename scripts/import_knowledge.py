"""知识库导入：data/knowledge/*.yaml → 数据库，并生成用户核对清单。

用法：
    python -m scripts.import_knowledge                # 导入 + 更新核对清单
    python -m scripts.import_knowledge --checklist    # 只更新核对清单，不碰数据库
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from jobs.knowledge import load_knowledge_dir
from jobs.knowledge.checklist import write_checklist
from jobs.knowledge.importer import import_knowledge

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
CHECKLIST_PATH = REPO_ROOT / "docs" / "知识库核对清单.md"


async def run_import() -> int:
    docs = load_knowledge_dir(KNOWLEDGE_DIR)
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            summary = await import_knowledge(session, docs)
    finally:
        await engine.dispose()
    print(f"导入完成：{summary}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="导入产业链知识库")
    parser.add_argument("--checklist", action="store_true", help="只生成核对清单")
    args = parser.parse_args()

    docs = load_knowledge_dir(KNOWLEDGE_DIR)
    out = write_checklist(docs, CHECKLIST_PATH)
    print(f"核对清单已更新：{out}")
    if args.checklist:
        return 0
    return asyncio.run(run_import())


if __name__ == "__main__":
    raise SystemExit(main())
