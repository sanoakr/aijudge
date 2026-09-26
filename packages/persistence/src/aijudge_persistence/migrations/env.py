"""Alembic の実行環境。

**接続先はアプリと同じ場所から取る**（`aijudge_persistence.database_url`）。
別に持つと、移行を当てた先とアプリが読む先がずれる ── そのずれは
「移行したのに直っていない」という形でしか現れず、原因が分からない。

`target_metadata` はアプリの模型そのもの。`--autogenerate` と、CI の
「移行の形とモデルの形が一致するか」の検査が、どちらもこれを見る。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from aijudge_persistence.engine import database_url
from aijudge_persistence.schema import Base

config = context.config
config.set_main_option("sqlalchemy.url", database_url())

target_metadata = Base.metadata


# 移行が 1 つのロックを待つ上限（PostgreSQL）。
MIGRATION_LOCK_TIMEOUT = "10s"


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite は ALTER が弱い。テストは `create_all` を使うので
            # ここを通らないが、開発機で SQLite に当てることはできる。
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            if connection.dialect.name == "postgresql":
                # **ロックを待ちすぎない**（#425）。移行はサービスの稼働中に
                # 走る。ALTER が長いトランザクションの後ろでロックを待つ間、
                # 後から来た全てのクエリがその ALTER の後ろに並び、画面ごと
                # 止まる。待てなければ移行を失敗させ、デプロイは記録を進めずに
                # 次の周回で再試行する（`deploy.sh`）。
                connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'")
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
