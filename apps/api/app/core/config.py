"""应用配置：全部来自环境变量，缺关键项立即失败。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行期配置。

    任何密钥、连接串都只能来自环境变量或本地 .env（.env 不入库）。
    缺少 DATABASE_URL / REDIS_URL 时在进程启动阶段直接抛错，避免"带病运行"。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    version: str = "0.1.0"

    database_url: str
    redis_url: str

    api_token: str = "change-me-in-production"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    worker_heartbeat_ttl_sec: int = 90
    model_dir: str = "models"  # GBM 产物目录（相对仓库根），gitignored

    # 资讯 LLM 抽取（可选）：无 key 时只用规则抽取，成本为 0
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_daily_budget_cny: float = 5.0
    llm_price_in_cny_per_1k: float = 0.001
    llm_price_out_cny_per_1k: float = 0.002

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL 不能为空")
        if not value.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError(
                "DATABASE_URL 必须是 postgresql:// 或 postgresql+asyncpg:// 开头"
                "（本项目不使用 SQLite）"
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("REDIS_URL 不能为空")
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL 必须是 redis:// 或 rediss:// 开头")
        return value


def load_settings() -> Settings:
    """显式加载配置，便于测试注入。"""
    return Settings()
