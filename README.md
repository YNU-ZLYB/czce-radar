# CZCE Radar v2

郑商所品种 · 预测与研判系统（个人决策辅助，企业级工程标准）

> 全新代码库。v1（`/workspace/futures-radar`）只提供实证结论与知识资产，**不继承任何代码结构**。

## 文档索引

| 文档 | 内容 |
|---|---|
| [00-需求与定位](docs/00-需求与定位.md) | v1 复盘、用户场景、27 条需求与验收标准、非目标、量化成功标准 |
| [01-系统架构](docs/01-系统架构.md) | 分层架构、模块边界、技术选型、一天的数据流、工程规范、部署拓扑 |
| [02-数据与预测引擎](docs/02-数据与预测引擎.md) | 表结构、数据源契约、预测目标定义、特征字典、模型方案、回测协议、LLM 规范 |
| [03-执行路线](docs/03-执行路线.md) | M0~M7 里程碑与 DoD、依赖图、风险登记、决策记录（ADR） |
| [04-界面规范](docs/04-界面规范.md) | 卡片规格、板块分组、双时间、配色 Token、交互与可用性验收 |
| [05-运维手册](docs/05-运维手册.md) | 环境变量、探针自愈、告警定位、回滚步骤、数据源替代方案、定时任务清单 |
| [refs/](docs/refs/) | v1 资产：产业链与传导手册、隔夜传导回测报告 |

## 核心一句话

**预测在 08:30 之前给出，只用昨天及更早的数据与隔夜外盘；资讯只用于盘中修订与解释，永不用于制造"预测"。**

## 目录结构

```
apps/api/app      FastAPI 接口层（只读、校验、SSE）
apps/web          Vue3 + TS + Vite + Vant4 PWA
jobs/             Arq Worker（采集 / 特征 / 预测 / 评分）
packages/schema   前后端共享契约（Pydantic）
migrations/       Alembic 迁移（含 23 条品种种子）
infra/            Dockerfile
tests/            pytest
```

## 快速开始（M0）

```bash
# 1) 起数据库、Redis、API、Worker（API 容器启动时自动 alembic upgrade head）
docker compose up --build

# 2) 一键验证部署（/live、/ready 三态、迁移与种子断言；结束后自动清理）
scripts/verify_deploy.sh

# 3) 另开终端，起前端
cd apps/web && npm ci && npm run dev
# 打开 http://localhost:5173 ，应看到 db / redis / worker 三态均为「正常」

# 4) 不用 Docker 的本地开发
cp .env.example .env          # 按需修改 DATABASE_URL / REDIS_URL
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/arq jobs.worker.WorkerSettings

# 5) M3 回测（先回补国内/外盘日线，再一键出报告）
.venv/bin/python -m scripts.backfill_daily
.venv/bin/python -m scripts.backfill_foreign
.venv/bin/python -m scripts.backtest      # → docs/backtest/YYYYMMDD.md + .json

# 6) M4 训练（LightGBM + isotonic 校准 + 融合；达标自动 active，否则 shadow）
.venv/bin/python -m scripts.train          # --dry-run 只评估；--activate 强制上线
```

### 两个探针

| 端点 | 用途 | 语义 |
|---|---|---|
| `/live` | 平台健康检查、容器 `HEALTHCHECK` | 不碰 db/redis，进程活着就 200 |
| `/ready` | 前端展示、运维排障 | db/redis 故障返回 503；worker 心跳缺失只降级为 200 `degraded` |

迁移由容器入口脚本执行（`RUN_MIGRATIONS=1`，worker 为 `0`），Render 无需额外 release 步骤。

云端连接串：Supabase 通常给 `?sslmode=require`，后端会自动转成 asyncpg 需要的 `?ssl=require`，直接粘贴即可。

质量门禁：`ruff check .` · `mypy` · `pytest` · `cd apps/web && npm run build`（CI 全量执行）。

## 当前状态

- [x] 方案设计（5 份文档）
- [x] M0 工程骨架（FastAPI + PostgreSQL + Redis + Arq + Vue3，CI 全绿）
- [x] M1 数据底座（`quote_1m` 日分区 + 新浪实时/日线采集 + 幂等 ingest + `/api/v1/sources`；美元指数改用 FRED `DTWEXBGS` 日线补齐）
- [x] M2 知识库与特征（21 份产业链 YAML 全部 `reviewed: true` + 导入器 + `FeatureSnapshot f1` + 外盘日线聚合 + 郑商所仓单日报采集）
- [x] M3 预测引擎 v1（Layer A 规则 + 08:30 预测 + walk-forward 回测 + `/api/v1/predictions`；v1 全样本方向 56.2%、生产规则 65.8%）
- [x] M4 预测引擎 v2 机制（LightGBM + isotonic 校准 + logloss 融合 + model_registry + 周一重训 + admin 切换/回滚；首版 `gbm-20261110-01` 未过上线门槛 → shadow，规则版继续服务）
- [x] M5 资讯层与预期差修订（四路采集 + 双层去重 + 规则抽取/可选 LLM + lag1 情绪入特征 + 修订/SSE/成本统计）
- [x] M6 产品前端（21 卡片首页 + 详情抽屉（趋势/贡献者/利多利空/修订/校准说明）+ 简报页 + Scorecard 页 + SSE 断线重连补齐 + PWA 离线缓存与过期提示；新增 `GET /api/v1/varieties[/{code}/detail]`、`/scorecard`、`/news/extracts`；outcome 每日 15:20 CST 事后评分）
- [~] M7 上线与观测（`GET /metrics` Prometheus、每日 scorecard 快照落库、周报对比、回滚演练、运维手册已交付；公网部署/7日观测/月度成本待上线后勾选）
