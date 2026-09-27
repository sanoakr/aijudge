"""保存のあとに一度だけ出す知らせ（`aijudge_reviewconsole.notices`・2026-09-28）。

以前は `Console.last_*` にコース単位で持ち、読んでも消さなかった。**操作した本人に
一度だけ**出す。
"""

from __future__ import annotations

from test_manage import World, _import_example, _unit_of
from test_manage import world as world

from aijudge_core import Role
from aijudge_reviewconsole.notices import JOBS_RELEASED, TASK_SAVED, Notices


def test_a_notice_is_taken_once_and_only_by_its_owner() -> None:
    notices = Notices()
    notices.put("usr_a", "crs_1", TASK_SAVED, "saved")
    assert notices.take("usr_b", "crs_1", TASK_SAVED) is None, "別の利用者に出た"
    assert notices.take("usr_a", "crs_2", TASK_SAVED) is None, "別のコースに出た"
    assert notices.take("usr_a", "crs_1", TASK_SAVED) == "saved"
    assert notices.take("usr_a", "crs_1", TASK_SAVED) is None, "2 度出た"


def test_a_scoped_notice_waits_for_its_own_task() -> None:
    notices = Notices()
    notices.put("usr_a", "crs_1", "test_case_error", "boom", scope="tsk_1")
    assert notices.take("usr_a", "crs_1", "test_case_error", scope="tsk_2") is None
    assert notices.take("usr_a", "crs_1", "test_case_error", scope="tsk_1") == "boom"


def test_unread_notices_do_not_pile_up() -> None:
    notices = Notices(limit=2)
    for n in range(3):
        notices.put("usr_a", f"crs_{n}", TASK_SAVED, n)
    assert notices.take("usr_a", "crs_0", TASK_SAVED) is None, "古いものが捨てられていない"
    assert notices.take("usr_a", "crs_2", TASK_SAVED) == 2


def test_another_instructor_does_not_see_my_notice(world: World) -> None:
    """**同じコースの別の教員には出ない。** 以前はコース単位で、他人の操作が見えた。"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    colleague = world.register("colleague", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/grade-now", follow_redirects=False
    )

    url = f"/manage/courses/{world.course.id}/units/{unit}"
    assert world.console.notices.take(colleague.user_id, world.course.id, JOBS_RELEASED) is None
    first = world.client("teacher").get(url)
    assert first.status_code == 200
    # 本人の分は画面で消費された。
    assert world.console.notices.take(teacher.user_id, world.course.id, JOBS_RELEASED) is None
