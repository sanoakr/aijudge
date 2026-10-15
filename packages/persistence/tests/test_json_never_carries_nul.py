"""JSON 列に NUL（U+0000）を書いても保存が落ちないことを固定する。

PostgreSQL の JSONB は NUL を保存できない。採点結果にはプログラムの出力が
そのまま入るので、NUL を出力する提出 1 件で採点の保存が落ち、決定的ワーカーが
プロセスごと止まった（2026-10-15）。SQLite は保存できてしまうので、
**PostgreSQL でも走らせないと意味がない**（`AIJUDGE_TEST_DATABASE_URL`）。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from aijudge_persistence import Database
from aijudge_persistence.engine import NUL_REPLACEMENT, dumps_without_nul
from aijudge_persistence.schema import CampusNetworkRow

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Database:
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def test_a_nul_in_a_json_column_is_saved_as_a_replacement_character(database) -> None:
    """プログラムの出力（`printf("%c", 0)` など）がそのまま入る形を模す。"""
    document = {
        "stdout": "ok\x00tail",
        "cases": [{"actual": "\x00"}],
        "key\x00": "value",
    }
    with database.unit_of_work() as uow:
        uow.session.add(CampusNetworkRow(tenant_id="ten_nul", cidrs=document, updated_at=NOW))
        uow.commit()

    with database.unit_of_work() as uow:
        stored = uow.session.scalars(
            select(CampusNetworkRow.cidrs).where(CampusNetworkRow.tenant_id == "ten_nul")
        ).one()
    assert stored == {
        "stdout": f"ok{NUL_REPLACEMENT}tail",
        "cases": [{"actual": NUL_REPLACEMENT}],
        f"key{NUL_REPLACEMENT}": "value",
    }


def test_text_without_nul_is_serialised_unchanged() -> None:
    """置き換えるのは NUL だけ。`\\u0000` と書かれた文字列（6 文字）は触らない。"""
    assert dumps_without_nul({"a": "\\u0000", "b": [1, None, True]}) == (
        '{"a": "\\\\u0000", "b": [1, null, true]}'
    )
