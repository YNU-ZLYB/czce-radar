"""导出 OpenAPI 契约供前端生成 TypeScript 类型（契约单一来源）。

用法：python -m scripts.export_openapi
产物：apps/web/openapi.json → npm run gen:api → src/types/api.gen.d.ts
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://codegen:codegen@localhost/codegen")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.main import create_app  # noqa: E402


def main() -> None:
    app = create_app()
    spec = app.openapi()
    out = ROOT / "apps" / "web" / "openapi.json"
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written: {out} ({len(json.dumps(spec))} bytes)")


if __name__ == "__main__":
    main()
