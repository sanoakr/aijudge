"""課題を誰に・いつ見せるか（`aijudge_core.access`）。

固定したいのは、`access.py` の表の**全マス**である。1 マスでも黙って変わると、
試験の内容が TA に出るか、学習者に公開前の問題文が出る。

加えて 2 つ。

空は公開済み   `opens_at` の無い課題は、いまと同じく誰にでも見える。
出せる ⊂ 見える   公開前に出せるのに見えない、という組み合わせを作らない。

出題先（`audience_group_ids`）について 3 つ。

空は全員       出題先の無い課題は、名簿に関係なく学習者に見える（従来どおり）。
和集合         出題先のどれか 1 つに入っていれば見える。
学習者だけ     教員・TA は名簿に入っていなくても見える。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import Role, Task, in_audience, may_see, may_submit_before_open
from aijudge_core.ids import CourseGroupId, CourseId, TaskId

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
BEFORE = NOW + timedelta(hours=1)  # 公開は 1 時間後
AFTER = NOW - timedelta(hours=1)  # 公開は 1 時間前


RETAKE = CourseGroupId("grp_" + "3" * 32)
CLASS_1 = CourseGroupId("grp_" + "4" * 32)
CLASS_2 = CourseGroupId("grp_" + "5" * 32)


def _task(
    *,
    opens_at: datetime | None,
    confidential: bool,
    audience: tuple[CourseGroupId, ...] = (),
) -> Task:
    return Task(
        id=TaskId("tsk_" + "1" * 32),
        course_id=CourseId("crs_" + "2" * 32),
        title="小テスト",
        opens_at=opens_at,
        confidential_until_open=confidential,
        audience_group_ids=audience,
    )


# (役割, 秘匿か, 公開前か) → 見えるか。`access.py` の表をそのまま写す。
TABLE = [
    (Role.INSTRUCTOR, False, True, True),
    (Role.INSTRUCTOR, False, False, True),
    (Role.INSTRUCTOR, True, True, True),
    (Role.INSTRUCTOR, True, False, True),
    (Role.ADMIN, False, True, True),
    (Role.ADMIN, False, False, True),
    (Role.ADMIN, True, True, True),
    (Role.ADMIN, True, False, True),
    (Role.ASSISTANT, False, True, True),
    (Role.ASSISTANT, False, False, True),
    (Role.ASSISTANT, True, True, False),
    (Role.ASSISTANT, True, False, True),
    (Role.LEARNER, False, True, False),
    (Role.LEARNER, False, False, True),
    (Role.LEARNER, True, True, False),
    (Role.LEARNER, True, False, True),
]


@pytest.mark.parametrize(("role", "confidential", "before_open", "visible"), TABLE)
def test_every_cell_of_the_table(
    role: Role, confidential: bool, before_open: bool, visible: bool
) -> None:
    task = _task(opens_at=BEFORE if before_open else AFTER, confidential=confidential)

    assert may_see(task, role, now=NOW) is visible


def test_the_table_covers_every_role() -> None:
    """役割が増えたら、表に行を足すまでここで落ちる。"""
    assert {row[0] for row in TABLE} == set(Role)


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("confidential", [False, True])
def test_a_task_without_an_opening_time_is_visible_to_everyone(
    role: Role, confidential: bool
) -> None:
    """**空は公開済み**（従来どおり）。日程を入れていない課題は誰にでも見える。"""
    assert may_see(_task(opens_at=None, confidential=confidential), role, now=NOW)


def test_the_opening_moment_counts_as_open() -> None:
    """公開時刻ちょうどは公開済み。試験の開始と同時に TA にも見える。"""
    task = _task(opens_at=NOW, confidential=True)

    assert may_see(task, Role.ASSISTANT, now=NOW)
    assert may_see(task, Role.LEARNER, now=NOW)


@pytest.mark.parametrize(
    ("role", "confidential", "allowed"),
    [
        (Role.INSTRUCTOR, False, True),
        (Role.INSTRUCTOR, True, True),
        (Role.ASSISTANT, False, True),
        # 秘匿の課題では、TA は公開前に試しの提出をできない（2026-09-24 決定）。
        (Role.ASSISTANT, True, False),
        (Role.LEARNER, False, False),
        (Role.LEARNER, True, False),
        # 受講登録の無いテナント管理者には広げない（#340 の従来どおり）。
        (Role.ADMIN, False, False),
        (Role.ADMIN, True, False),
    ],
)
def test_who_may_submit_before_the_opening(role: Role, confidential: bool, allowed: bool) -> None:
    task = _task(opens_at=BEFORE, confidential=confidential)

    assert may_submit_before_open(task, role, now=NOW) is allowed


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("confidential", [False, True])
def test_whoever_may_submit_before_opening_may_also_see(role: Role, confidential: bool) -> None:
    """出せるのに見えない、を作らない。"""
    task = _task(opens_at=BEFORE, confidential=confidential)

    if may_submit_before_open(task, role, now=NOW):
        assert may_see(task, role, now=NOW)


# (役割, 秘匿か, 公開前か) → 出題先の**外**の人に見えるか。表の最後の行と、
# 出題先が教員・TA に効かないこと。
OUTSIDE_TABLE = [
    (Role.INSTRUCTOR, False, False, True),
    (Role.ADMIN, False, False, True),
    (Role.ASSISTANT, False, False, True),
    (Role.ASSISTANT, True, True, False),  # 秘匿は出題先と独立に効く
    (Role.LEARNER, False, True, False),
    (Role.LEARNER, False, False, False),
    (Role.LEARNER, True, True, False),
    (Role.LEARNER, True, False, False),
]


@pytest.mark.parametrize(("role", "confidential", "before_open", "visible"), OUTSIDE_TABLE)
def test_someone_outside_the_audience(
    role: Role, confidential: bool, before_open: bool, visible: bool
) -> None:
    task = _task(
        opens_at=BEFORE if before_open else AFTER, confidential=confidential, audience=(RETAKE,)
    )

    assert may_see(task, role, now=NOW, groups=frozenset()) is visible


def test_a_learner_in_the_audience_sees_it_once_open() -> None:
    task = _task(opens_at=AFTER, confidential=False, audience=(RETAKE,))

    assert may_see(task, Role.LEARNER, now=NOW, groups=frozenset({RETAKE}))


def test_the_audience_does_not_open_a_set_early() -> None:
    """名簿に入っていても、公開前は見えない。出題先は公開を早めない。"""
    task = _task(opens_at=BEFORE, confidential=False, audience=(RETAKE,))

    assert not may_see(task, Role.LEARNER, now=NOW, groups=frozenset({RETAKE}))


def test_an_empty_audience_means_everyone() -> None:
    """**空は全員**（従来どおり）。名簿に何も入っていない学習者にも見える。"""
    task = _task(opens_at=AFTER, confidential=False)

    assert in_audience(task, frozenset())
    assert may_see(task, Role.LEARNER, now=NOW, groups=frozenset())


def test_several_groups_mean_any_of_them() -> None:
    """共通問題を 1 組と 2 組の両方に出す（グループごとの出し分け）。"""
    shared = _task(opens_at=AFTER, confidential=False, audience=(CLASS_1, CLASS_2))

    assert in_audience(shared, frozenset({CLASS_1}))
    assert in_audience(shared, frozenset({CLASS_2}))
    assert not in_audience(shared, frozenset({RETAKE}))
