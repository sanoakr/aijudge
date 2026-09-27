"""位置の無い課題に位置を振る移行（#484）。

移行の直前の形の DB に課題を入れ、移行を当てて確かめる。列と文書の両方が
書かれていること、位置のある課題は動かないこと、作成順（版 1 の作成日時）で
末尾から振られることを見る。

**PostgreSQL でも流す**（`AIJUDGE_TEST_DATABASE_URL` があれば）。本番の文書は
JSONB で、作成日時は文字列ではなく datetime で返る ── SQLite だけでは、その
2 つの扱いを確かめられない。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[3]
BEFORE = "c4e8a2f6d1b7"
FILLS = "aa5e81406d1c"
COURSE = "crs_" + "1" * 32
OTHER_COURSE = "crs_" + "2" * 32


def _config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """移行の直前の形の DB。`env.py` は URL を環境変数から読む。"""
    if request.param == "postgres":
        assert POSTGRES_URL is not None
        url = POSTGRES_URL
        # 空から積み上げる（`test_migrations.py` と同じ理由・#243）。
        engine = sa.create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("DROP SCHEMA public CASCADE"))
                connection.execute(sa.text("CREATE SCHEMA public"))
        finally:
            engine.dispose()
    else:
        url = f"sqlite+pysqlite:///{tmp_path}/m.db"
    previous = os.environ.get("AIJUDGE_DATABASE_URL")
    os.environ["AIJUDGE_DATABASE_URL"] = url
    try:
        command.upgrade(_config(url), BEFORE)
        yield url
    finally:
        if previous is None:
            os.environ.pop("AIJUDGE_DATABASE_URL", None)
        else:
            os.environ["AIJUDGE_DATABASE_URL"] = previous


def _task(
    connection: sa.Connection,
    task_id: str,
    *,
    course: str = COURSE,
    unit: str | None = "ex01",
    position: int | None = None,
    created: str | None = None,
) -> None:
    document = {"id": task_id, "course_id": course, "title": task_id, "unit": unit}
    if position is not None:
        document["position"] = position
    connection.execute(
        sa.text(
            "insert into tasks (id, course_id, unit, session, position, document)"
            " values (:id, :course, :unit, null, :position, :document)"
        ),
        {
            "id": task_id,
            "course": course,
            "unit": unit,
            "position": position,
            "document": json.dumps(document),
        },
    )
    if created is not None:
        connection.execute(
            sa.text(
                "insert into task_versions (id, task_id, version, subject_profile,"
                " review_state, allow_handwriting, statement, created_at, document)"
                " values (:id, :task, 1, 'p', 'approved', false, 's', :created, '{}')"
            ),
            {"id": "tsv_" + task_id[4:], "task": task_id, "created": created},
        )


def _positions(url: str) -> dict[str, tuple[int | None, int | None]]:
    """課題 ID → (列の位置, 文書の位置)。"""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text("select id, position, document from tasks"))
            # SQLite は文字列、PostgreSQL（JSONB）は dict で返す。
            return {
                row[0]: (
                    row[1],
                    (json.loads(row[2]) if isinstance(row[2], str) else row[2]).get("position"),
                )
                for row in rows.fetchall()
            }
    finally:
        engine.dispose()


def _fill(url: str, *tasks: dict[str, object]) -> dict[str, tuple[int | None, int | None]]:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            for task in tasks:
                _task(connection, **task)  # type: ignore[arg-type]
    finally:
        engine.dispose()
    command.upgrade(_config(url), FILLS)
    return _positions(url)


def test_missing_positions_go_after_the_last_in_creation_order(url: str) -> None:
    found = _fill(
        url,
        {"task_id": "tsk_a", "position": 1, "created": "2026-09-01 00:00:00"},
        {"task_id": "tsk_b", "position": 2, "created": "2026-09-02 00:00:00"},
        # 作成順は c → d。ID の順（d < c ではない）と取り違えていないかも見る。
        {"task_id": "tsk_d", "created": "2026-09-04 00:00:00"},
        {"task_id": "tsk_c", "created": "2026-09-03 00:00:00"},
    )
    assert found["tsk_c"] == (3, 3)
    assert found["tsk_d"] == (4, 4)


def test_tasks_with_a_position_are_not_moved(url: str) -> None:
    """教員が並べた順を、移行が変えてはいけない。番号の飛びもそのまま。"""
    found = _fill(
        url,
        {"task_id": "tsk_a", "position": 5, "created": "2026-09-01 00:00:00"},
        {"task_id": "tsk_b", "position": 2, "created": "2026-09-02 00:00:00"},
        {"task_id": "tsk_c", "created": "2026-09-03 00:00:00"},
    )
    assert found["tsk_a"] == (5, 5)
    assert found["tsk_b"] == (2, 2)
    assert found["tsk_c"] == (6, 6)


def test_each_unit_and_course_is_counted_on_its_own(url: str) -> None:
    found = _fill(
        url,
        {"task_id": "tsk_a", "position": 3, "created": "2026-09-01 00:00:00"},
        {"task_id": "tsk_b", "unit": "ex02", "created": "2026-09-02 00:00:00"},
        {"task_id": "tsk_c", "unit": None, "created": "2026-09-03 00:00:00"},
        {"task_id": "tsk_d", "course": OTHER_COURSE, "created": "2026-09-04 00:00:00"},
    )
    assert found["tsk_b"] == (1, 1)
    assert found["tsk_c"] == (1, 1)
    assert found["tsk_d"] == (1, 1)


def test_ties_and_tasks_without_versions_are_ordered_by_id(url: str) -> None:
    """同着は ID で割る。版の無い課題は作成時刻が分からないので最後に。"""
    found = _fill(
        url,
        {"task_id": "tsk_z"},
        {"task_id": "tsk_b", "created": "2026-09-02 00:00:00"},
        {"task_id": "tsk_a", "created": "2026-09-02 00:00:00"},
    )
    assert [task for task, _ in sorted(found.items(), key=lambda item: item[1][0] or 0)] == [
        "tsk_a",
        "tsk_b",
        "tsk_z",
    ]
