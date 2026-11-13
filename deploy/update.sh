#!/usr/bin/env bash
# 一键更新并回补：同步代码 → 重建 → 重启 → 等就绪 → 补当日数据 → 验证
# 用法：sudo bash deploy/update.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE=(docker compose -f "$SCRIPT_DIR/docker-compose.prod.yml")

cd "$REPO_DIR"

step() { printf '\n==> %s\n' "$1"; }

step "1/6 同步代码"
git fetch --quiet origin main
git reset --hard FETCH_HEAD
git log --oneline -1

step "2/6 重建镜像（api/worker/web）"
"${COMPOSE[@]}" build api worker web

step "3/6 重启服务"
"${COMPOSE[@]}" up -d

step "4/6 等待 /ready"
ready=0
for _ in $(seq 1 80); do
  if curl -fsS --max-time 2 http://localhost/ready >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 3
done
if [ "$ready" -eq 1 ]; then
  echo "ready ok"
else
  echo "!! /ready 超时，输出最近日志："
  "${COMPOSE[@]}" logs --tail=40 api
  exit 1
fi

step "5/6 回补当日数据（日线 → 快照 → 预测 → 评分 → 看板）"
"${COMPOSE[@]}" exec -T worker python - <<'PY'
import asyncio

import httpx

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
import jobs.worker as w

ORDER = [
    w.collect_daily_bars,
    w.build_snapshots,
    w.predict_daily,
    w.score_daily,
    w.scorecard_daily,
]

async def main() -> None:
    settings = load_settings()
    engine = create_engine(settings)
    ctx = {
        "engine": engine,
        "session_factory": create_session_factory(engine),
        "http": httpx.AsyncClient(timeout=httpx.Timeout(30.0)),
    }
    try:
        for fn in ORDER:
            try:
                print(f"{fn.__name__:<20} => {str(await fn(ctx))[:160]}")
            except Exception as exc:  # 单个任务失败不阻断后续
                print(f"{fn.__name__:<20} ERR {type(exc).__name__}: {exc}"[:200])
    finally:
        await ctx["http"].aclose()
        await engine.dispose()

asyncio.run(main())
PY

step "6/6 验证"
"${COMPOSE[@]}" exec -T db psql -U "${POSTGRES_USER:-czce}" -d "${POSTGRES_DB:-czce}" -c "
SELECT (SELECT max(trade_date) FROM daily_bar)  AS bar_max,
       (SELECT count(DISTINCT variety_code) FROM daily_bar
         WHERE trade_date = (SELECT max(trade_date) FROM daily_bar)) AS bar_varieties,
       (SELECT count(*) FROM prediction)         AS pred_n,
       (SELECT max(target_date) FROM prediction) AS pred_max,
       (SELECT count(*) FROM outcome)            AS outcome_n,
       (SELECT count(*) FROM news_extract WHERE method = 'llm') AS llm_extract_n;"

echo
echo "完成。打开 http://$(hostname -I | awk '{print $1}')/ 查看。"
