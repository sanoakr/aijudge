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
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
