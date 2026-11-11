#!/usr/bin/env bash
# 本机 Docker 部署验证（M1/M2 DoD）：
#   docker compose up --build → 轮询 /live、/ready → 断言迁移与种子 → 清理
# 用法：scripts/verify_deploy.sh          # 结束后 down -v
#       KEEP=1 scripts/verify_deploy.sh   # 保留容器与数据卷，便于排查
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

API_BASE="${API_BASE:-http://localhost:8000}"
KEEP="${KEEP:-0}"

cleanup() {
  if [ "$KEEP" = "1" ]; then
    echo "[info] KEEP=1，保留容器：docker compose ps"
    return
  fi
  echo "[info] docker compose down -v"
  docker compose down -v --remove-orphans
}
trap cleanup EXIT

echo "==> docker compose up --build -d"
docker compose up --build -d

wait_for() {
  local url="$1" needle="$2" name="$3" tries="${4:-90}"
  local body=""
  for _ in $(seq 1 "$tries"); do
    body="$(curl -fsS "$url" 2>/dev/null || true)"
    if printf '%s' "$body" | grep -q "$needle"; then
      echo "[ok] $name $body"
      return 0
    fi
    sleep 2
  done
  echo "[fail] $name 未就绪（$url -> $body）" >&2
  docker compose ps >&2 || true
  docker compose logs --tail=100 api worker db redis >&2 || true
  return 1
}

# /live 不依赖 db/redis；/ready 要求 db+redis 均 ok（worker 仅降级展示）
wait_for "$API_BASE/live" '"status":"alive"' /live 30
wait_for "$API_BASE/ready" '"db":"ok"' /ready
wait_for "$API_BASE/ready" '"redis":"ok"' /ready

# worker 心跳由 Arq 启动时写入（TTL 90s），确认容器确实在跑任务
wait_for "$API_BASE/ready" '"worker":"ok"' /ready 60

echo "==> 断言迁移与种子数据"
mig="$(docker compose exec -T db psql -U czce -d czce -tAc 'select version_num from alembic_version')"
[ -n "$mig" ] || { echo "[fail] alembic_version 为空，迁移未执行" >&2; exit 1; }
echo "[ok] alembic head = $mig"

tradable="$(docker compose exec -T db psql -U czce -d czce -tAc 'select count(*) from variety where is_tradable')"
[ "$tradable" = "21" ] || { echo "[fail] is_tradable=$tradable（期望 21）" >&2; exit 1; }
echo "[ok] 可交易品种 = 21"

echo "全部通过"
