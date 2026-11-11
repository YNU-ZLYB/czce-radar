"""market tables: quote_1m (partitioned), daily_bar, foreign_daily, source, fetch_log

Revision ID: 0003_market_tables
Revises: 0002_seed_varieties
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_market_tables"
down_revision: str | None = "0002_seed_varieties"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENSURE_PARTITION_FN = """
CREATE OR REPLACE FUNCTION czce_ensure_quote_partition(target date) RETURNS void AS $fn$
DECLARE
    part text;
BEGIN
    part := 'quote_1m_' || to_char(target, 'YYYYMMDD');
    IF to_regclass(format('%I', part)) IS NULL THEN
        -- 分区边界固定为 UTC 零点，与写入侧 (ts UTC date) 一致，
        -- 不依赖执行会话的 TimeZone 设置
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF quote_1m '
            'FOR VALUES FROM (%L::timestamptz) TO (%L::timestamptz)',
            part, target::text || ' 00:00:00+00', (target + 1)::text || ' 00:00:00+00'
        );
    END IF;
END
$fn$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # 分钟行情：按日 RANGE 分区；唯一键必须包含分区键 ts
    op.execute("""
        CREATE TABLE quote_1m (
            id           bigserial,
            market       char(1)       NOT NULL,
            symbol       varchar(16)   NOT NULL,
            variety_code varchar(8),
            price        numeric(18,4) NOT NULL,
            prev_close   numeric(18,4),
            open         numeric(18,4),
            high         numeric(18,4),
            low          numeric(18,4),
            volume       bigint,
            oi           bigint,
            ts           timestamptz   NOT NULL,
            source       varchar(24)   NOT NULL,
            created_at   timestamptz   NOT NULL DEFAULT now(),
            CONSTRAINT uq_quote_1m_source_symbol_ts UNIQUE (source, symbol, ts)
        ) PARTITION BY RANGE (ts)
    """)
    op.execute("CREATE INDEX ix_quote_1m_ts_brin ON quote_1m USING brin (ts)")
    op.execute("CREATE INDEX ix_quote_1m_symbol_ts ON quote_1m (symbol, ts DESC)")
    op.execute("CREATE INDEX ix_quote_1m_variety_ts ON quote_1m (variety_code, ts DESC)")
    op.execute(_ENSURE_PARTITION_FN)
    # 当天与次日分区（worker 每日还会滚动预建）
    op.execute("SELECT czce_ensure_quote_partition(current_date)")
    op.execute("SELECT czce_ensure_quote_partition(current_date + 1)")

    op.create_table(
        "daily_bar",
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 4), nullable=False),
        sa.Column("high", sa.Numeric(18, 4), nullable=False),
        sa.Column("low", sa.Numeric(18, 4), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("settle", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint("variety_code", "trade_date", name="pk_daily_bar"),
    )

    op.create_table(
        "foreign_daily",
        sa.Column("ext_code", sa.String(8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("change_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint("ext_code", "trade_date", name="pk_foreign_daily"),
    )

    op.create_table(
        "source",
        sa.Column("code", sa.String(24), nullable=False),
        sa.Column("name", sa.String(48), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("interval_sec", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_status", sa.String(12), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("code", name="pk_source"),
    )

    op.create_table(
        "fetch_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_code", sa.String(24), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("fetched", sa.Integer(), nullable=True),
        sa.Column("inserted", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_fetch_log"),
    )
    op.create_index(
        "ix_fetch_log_source_started",
        "fetch_log",
        ["source_code", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_fetch_log_source_started", table_name="fetch_log")
    op.drop_table("fetch_log")
    op.drop_table("source")
    op.drop_table("foreign_daily")
    op.drop_table("daily_bar")
    op.execute("DROP FUNCTION IF EXISTS czce_ensure_quote_partition(date)")
    op.execute("DROP TABLE IF EXISTS quote_1m CASCADE")
