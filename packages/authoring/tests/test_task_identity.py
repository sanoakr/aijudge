"""課題 ID はコースごとに別（#70）。

ID がキーだけから導かれていたので、2 つのコースが同じ自然な鍵
（`ex01/p1`）を使うと同じ課題 ID になった。**落ちるほうがまだ良かった** ──
内容が同じだと落ちず、`save_task` が `Task` 行を上書きして、既存の課題が
黙って別のコースへ移る。
"""

from __future__ import annotations

from aijudge_authoring import TaskSpec, build_task_version
from aijudge_core.ids import CourseId, UserId

AUTHOR = UserId("usr_" + "1" * 32)
FIRST = CourseId("crs_" + "1" * 32)
SECOND = CourseId("crs_" + "2" * 32)
SPEC = TaskSpec(key="ex01/p1", statement="## 課題 ##\n\n本文")


def _version(course: CourseId):
    return build_task_version(
        SPEC, course_id=course, subject_profile="cs_intro_c", authored_by=AUTHOR
    )


def test_the_same_key_in_two_courses_is_two_tasks() -> None:
    """**同じ鍵は普通に起きる。** 同じ演習を別の学科でも出す、など。"""
    first, second = _version(FIRST), _version(SECOND)
    assert first.task_id != second.task_id
    assert first.id != second.id


def test_the_same_key_in_one_course_is_still_the_same_task() -> None:
    """**取り込みの冪等性は壊さない。** 何度流しても課題は増えない。"""
    assert _version(FIRST).task_id == _version(FIRST).task_id
    assert _version(FIRST).id == _version(FIRST).id
