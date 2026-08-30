"""移行の形とモデルの形が一致することを機械で確かめる。

**このテストが無いと、モデルを変えて移行を書き忘れても誰も気づかない。**
実際に起きた ── `courses` に列を 1 つ足した変更は**全テスト green のまま**
通り抜け、稼働中の DB で全てのコース照会が落ちた（#24）。テストは毎回
新しい DB を `create_all` で作るので、既存テーブルに列が足りない状態を
再現できない。

だから比べるのは 2 つの形である。

    空の DB に `alembic upgrade head` を当てた形
    モデル（`Base.metadata`）が言う形

ずれていれば、移行を書き忘れたか、書いた移行がモデルと違う。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from aijudge_persistence.schema import Base

REPO_ROOT = Path(__file__).resolve().parents[3]


def _config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_the_migrations_build_what_the_models_describe(tmp_path: Path) -> None:
    """**移行だけで作った形が、モデルの言う形と一致する。**

    SQLite で確かめる。方言固有の型（PostgreSQL の JSONB）は variant として
    宣言されているので、SQLite では JSON に落ちる ── 両方の形が同じ宣言から
    出ていることを確かめるには、どちらの方言でも成立していればよい。
    """
    url = f"sqlite+pysqlite:///{tmp_path}/m.db"
    previous = os.environ.get("AIJUDGE_DATABASE_URL")
    os.environ["AIJUDGE_DATABASE_URL"] = url
    try:
        command.upgrade(_config(url), "head")
        engine = sa.create_engine(url)
        try:
            with engine.connect() as connection:
                diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        finally:
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("AIJUDGE_DATABASE_URL", None)
        else:
            os.environ["AIJUDGE_DATABASE_URL"] = previous

    assert diff == [], (
        "移行とモデルがずれています。モデルを変えたなら移行を足してください:\n"
        "  uv run alembic revision --autogenerate -m '何を変えたか'\n"
        f"差分: {diff}"
    )


def test_every_revision_is_reachable_from_head() -> None:
    """**枝分かれしていないこと。** 2 つの head があると、どちらを当てたかで
    形が変わる。
    """
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(_config("sqlite://")).get_heads()
    assert len(heads) == 1, f"head が {len(heads)} つあります: {heads}"


@pytest.mark.skipif(
    not os.environ.get("AIJUDGE_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL が要る（AIJUDGE_TEST_DATABASE_URL で指定）",
)
def test_the_migrations_also_build_the_postgres_shape() -> None:
    """PostgreSQL でも一致すること。**本番はこちらで動く。**

    JSONB は PostgreSQL でしか現れないので、SQLite だけでは確かめられない。
    """
    url = os.environ["AIJUDGE_TEST_DATABASE_URL"]
    command.upgrade(_config(url), "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert diff == []
