"""教員の問題セット一覧を、学生の画面と同じ段階で分ける（2026-10-10）。

「学生に公開中」の 1 本では、いま出せるセットと受付を終えたセットが混ざる。
学期の後半には終わった回が十数行並び、その中から「いまの回」を探すことになる。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from test_manage import World, _import_example
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId


def _schedule(world: World, task_id: str, **dates: datetime | None) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        uow.tasks.save_task(task.model_copy(update=dates))
        uow.commit()


def _stage_headings(page: str) -> list[str]:
    """段階の見出しを、並んでいる順に返す。"""
    marker = 'class="sets-stage sets-stage-'
    return [chunk.split('"', 1)[0] for chunk in page.split(marker)[1:]]


def test_a_set_open_for_submission_is_listed_as_such(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    now = datetime.now(UTC)
    _schedule(world, task_id, opens_at=now - timedelta(days=1), due_at=now + timedelta(days=1))

    page = world.client("teacher").get(f"/courses/{world.course.id}").text

    assert "学生に公開中" in page
    assert _stage_headings(page) == ["open"]
    assert "提出できる" in page


def test_a_set_past_its_end_moves_to_closed(world: World) -> None:
    """受付終了を過ぎたら「受付を終了した」へ。学生の一覧と同じ判定（`set_window_at`）。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    now = datetime.now(UTC)
    _schedule(
        world,
        task_id,
        opens_at=now - timedelta(days=9),
        due_at=now - timedelta(days=2),
        accepts_until=now - timedelta(days=1),
    )

    page = world.client("teacher").get(f"/courses/{world.course.id}").text

    assert _stage_headings(page) == ["closed"]
    assert "受付を終了した" in page


def test_between_the_deadline_and_the_end_is_late(world: World) -> None:
    """締切では閉じない（ADR 0013）── 減点して出せる間は受付終了と分ける。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    now = datetime.now(UTC)
    _schedule(
        world,
        task_id,
        opens_at=now - timedelta(days=9),
        due_at=now - timedelta(hours=1),
        accepts_until=now + timedelta(days=1),
    )

    page = world.client("teacher").get(f"/courses/{world.course.id}").text

    assert _stage_headings(page) == ["late"]


def test_sets_hidden_from_learners_are_not_split(world: World) -> None:
    """段階で分けるのは学生に公開中の帯だけ。未公開のセットは学生の段階を持たない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    now = datetime.now(UTC)
    _schedule(world, task_id, opens_at=now + timedelta(days=1), due_at=now + timedelta(days=8))

    page = world.client("teacher").get(f"/courses/{world.course.id}").text

    assert "未公開（TA まで見える）" in page
    assert _stage_headings(page) == []


def test_the_end_and_the_time_left_are_shown_as_for_learners(world: World) -> None:
    """受付終了と残り時間も学生の一覧と同じく出す。秒数はサーバが数える（#73）。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    now = datetime.now(UTC)
    _schedule(
        world,
        task_id,
        opens_at=now - timedelta(days=1),
        due_at=now + timedelta(hours=2),
        accepts_until=now + timedelta(days=1),
    )

    page = world.client("teacher").get(f"/courses/{world.course.id}").text

    assert "受付終了 " in page
    match = re.search(r'data-deadline-in="(-?\d+)"\s+data-state="(\w+)"', page)
    assert match, "残り時間の欄が無い"
    seconds, state = int(match.group(1)), match.group(2)
    assert 7000 < seconds <= 7200
    assert state == "open"
    # 数える script は学生の画面と同じものを読む。
    assert "countdown.js" in page
