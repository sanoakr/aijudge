"""問題セット（回）単位の値が、課題を作る・動かす経路のどれでも揃う（2026-11-14・#709）。

学内限定・公開まで秘匿・試験・出題先・受付終了などは、全課題に同じ値を書く設計である。
ところが経路ごとに「どの欄を写すか」が別々に書かれていて、経路によって一部の欄が既定値に戻った。
例外も警告も出ない ── 公開前のはずの課題が TA に見える、学内限定が外れる、試験の印が付かない。

固定したいのは 4 つ。

移す          別のセットへ移した課題は、移した先のセットの値を全部持つ
取り込み直す  `task import` の流し直しは、画面や定義で入れた課題の値を消さない
断られても    `course apply` が途中の課題で断られても、保存済みの新しい課題に
              秘匿・学内限定・試験の印は入っている
動画は出せる   セットがファイル選択を止めていても、動画を受ける課題は止めない
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring import TaskSpec
from aijudge_core import UNIT_WIDE_FIELDS, AnswerMode, FailedInputs, Task, unit_wide_values
from aijudge_core.ids import CourseGroupId, TenantId, UserId
from aijudge_course_admin.answer_mode import unit_values_for
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.course_definition import apply_course_definition
from aijudge_course_admin.operations import AdminError, ensure_course, import_tasks
from aijudge_course_admin.task_relocation import move_task
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"
TENANT = TenantId("ten_" + "0" * 32)
AUTHOR = UserId("usr_" + "a" * 32)
DUE = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)
OPENS = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)
GROUP = CourseGroupId("grp_" + "b" * 32)

#: 試験のセットに付ける値。既定と必ず違う値にする（既定のままなら取りこぼしが見えない）。
EXAM_VALUES: dict[str, object] = {
    "campus_only": True,
    "campus_ranges": ("10.0.0.0/24",),
    "confidential_until_open": True,
    "exam": True,
    "audience_group_ids": (GROUP,),
    "answer_mode": AnswerMode.EDITOR,
    "editor_completion": True,
    "file_upload": False,
    "screen_capture": True,
    "clear_points": 50.0,
    "failed_inputs": FailedInputs.NEVER,
    "results_after_review": True,
    "accepts_until": DUE,
    "grading_starts_at": DUE,
    "auto_finalize_after_minutes": 90,
}


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/unit-values.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    obj, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    return obj


def _task(database: Database, course, key: str, unit: str):
    return save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key=key, statement=f"## [必須] {key} ##\n\n本文", unit=unit, readability_weight=0.3
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=AUTHOR,
    )


def _set(database: Database, task_id, **fields) -> None:
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        assert task is not None
        uow.tasks.save_task(Task.model_validate(task.model_dump() | fields))
        uow.commit()


def _load(database: Database, task_id) -> Task:
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
    assert task is not None
    return task


def test_the_shared_list_covers_the_fields_the_unit_screens_set() -> None:
    """一覧はここ 1 か所。足し忘れを見つけるため、期待する欄をテストにも書く。"""
    expected = {
        "session", "opens_at", "submissions_open_at", "due_at", "grading_starts_at",
        "accepts_until", "auto_finalize_after_minutes", "late_penalty_steps", "campus_only",
        "campus_ranges", "confidential_until_open", "exam", "audience_group_ids", "answer_mode",
        "editor_completion", "file_upload", "screen_capture", "clear_points", "failed_inputs",
        "results_after_review",
    }  # fmt: skip
    assert set(UNIT_WIDE_FIELDS) == expected
    # 課題ごとに決まる値を、セットに揃えてしまわない。
    assert not {"accepted_suffixes", "case_timeout_seconds", "editor_template"} & set(
        UNIT_WIDE_FIELDS
    )


def test_moving_into_a_restricted_unit_carries_every_unit_value(database: Database, course) -> None:
    head = _task(database, course, "exam1/q1", "exam1")
    _set(database, head.task.id, opens_at=OPENS, due_at=DUE, **EXAM_VALUES)
    plain = _task(database, course, "ex01/p1", "ex01")

    moved = move_task(database, task_id=plain.task.id, unit="exam1")

    target = _load(database, head.task.id)
    carried = _load(database, moved.task.id)
    for name in UNIT_WIDE_FIELDS:
        assert getattr(carried, name) == getattr(target, name), f"{name} が移した先に揃っていない"
    # 念のため、値そのものも見る（両方が既定のまま、で通らないように）。
    assert carried.campus_only and carried.confidential_until_open and carried.exam
    assert carried.campus_ranges == ("10.0.0.0/24",)
    assert carried.audience_group_ids == (GROUP,)


def test_moving_out_of_a_restricted_unit_drops_the_restrictions(database: Database, course) -> None:
    """逆向き。試験のセットから演習へ移すと、演習の課題に学内限定と試験の印が残らない。"""
    plain_head = _task(database, course, "ex01/p1", "ex01")
    exam = _task(database, course, "exam1/q1", "exam1")
    _set(database, exam.task.id, opens_at=OPENS, due_at=DUE, **EXAM_VALUES)

    moved = move_task(database, task_id=exam.task.id, unit="ex01")

    carried = _load(database, moved.task.id)
    target = _load(database, plain_head.task.id)
    for name in UNIT_WIDE_FIELDS:
        assert getattr(carried, name) == getattr(target, name), f"{name} が移した先に揃っていない"
    assert not carried.campus_only and not carried.exam and not carried.confidential_until_open


def test_a_task_moved_into_an_empty_unit_keeps_its_own_values(database: Database, course) -> None:
    """揃える相手が居ないなら、いまの値のまま入る（今までどおり）。"""
    exam = _task(database, course, "exam1/q1", "exam1")
    _set(database, exam.task.id, opens_at=OPENS, due_at=DUE, **EXAM_VALUES)

    moved = move_task(database, task_id=exam.task.id, unit="exam2")

    assert _load(database, moved.task.id).confidential_until_open is True


def test_a_video_task_is_not_cut_off_by_a_unit_that_stops_file_selection(
    database: Database, course
) -> None:
    head = _task(database, course, "exam1/q1", "exam1")
    _set(
        database,
        head.task.id,
        opens_at=OPENS,
        due_at=DUE,
        answer_mode=AnswerMode.EDITOR,
        file_upload=False,
    )
    head_task = _load(database, head.task.id)
    plain = _load(database, _task(database, course, "ex01/p1", "ex01").task.id)

    assert unit_values_for(head_task, plain, course)["file_upload"] is False
    video = plain.model_copy(update={"accepted_suffixes": (".mp4",)})
    assert unit_values_for(head_task, video, course)["file_upload"] is True
    assert unit_wide_values(head_task)["file_upload"] is False


def test_reimporting_keeps_what_was_set_on_the_task_afterwards(database: Database, course) -> None:
    """`task import` は学期の頭に何度も流し直す。流し直しで課題の値を消さない。"""
    import_tasks(database, course_id=course.id, directory=EXAMPLE_TASK, profiles_dir=PROFILES)
    with database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(course.id)
    _set(
        database,
        task.id,
        opens_at=OPENS,
        due_at=DUE,
        withdrawn=True,
        accepted_suffixes=(".c", ".h"),
        **EXAM_VALUES,
    )
    before = _load(database, task.id)

    import_tasks(database, course_id=course.id, directory=EXAMPLE_TASK, profiles_dir=PROFILES)

    after = _load(database, task.id)
    assert after.model_dump() == before.model_dump(), "取り込み直しで課題の値が変わった"
    assert after.withdrawn and after.campus_only and after.accepted_suffixes == (".c", ".h")


DEFINITION = """
course:
  code: prog2
  title: プログラミング演習 II
  term: 2026-後期
  subject_profile: cs_lang_c_intro
units:
  exam1:
    opens_at: 2026-12-01T09:00:00+09:00
    due_at: 2026-12-01T10:00:00+09:00
    confidential_until_open: true
    campus_only: true
    exam: true
tasks:
  - key: ex01/old
    unit: ex01
    statement: |
      ## [必須] 古い課題 ##

      本文 {body}
    criteria:
      - code: shown
        title: 確認
        description: 読めるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {{level: 0, label: 不足, descriptor: 読めない, score_ratio: 0.0}}
          - {{level: 1, label: 十分, descriptor: 読める, score_ratio: 1.0}}
  - key: exam1/new
    unit: exam1
    statement: |
      ## [必須] 新しい試験の課題 ##

      本文
    criteria:
      - code: shown
        title: 確認
        description: 読めるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {{level: 0, label: 不足, descriptor: 読めない, score_ratio: 0.0}}
          - {{level: 1, label: 十分, descriptor: 読める, score_ratio: 1.0}}
"""


def test_a_refusal_midway_still_protects_the_new_exam_tasks(
    database: Database, tmp_path: Path
) -> None:
    """内容が違う既存課題で断られても、すでに保存した新しい試験の課題は見えたままにならない。

    以前は課題を 1 件ずつ保存してから回の値を入れたので、途中で断られると、保存済みの新しい
    課題が「秘匿なし・学内限定なし・試験でない」のまま残った。
    """
    path = tmp_path / "course.yaml"
    # 古い課題を先に入れておく。
    first = tmp_path / "first.yaml"
    first.write_text(DEFINITION.format(body="（初版）").replace("exam1/new", "exam1/skip"))
    apply_course_definition(
        database, tenant_id=TENANT, path=first, profiles_dir=PROFILES, authored_by=AUTHOR
    )
    # 古い課題の本文を変えた定義を、--revise なしで流す。新しい課題は古い課題より後ろに並ぶので、
    # 古い課題で断られるときには、新しい課題はまだ保存されていない。順序を逆にして保存を先にする。
    text = DEFINITION.format(body="（直した）")
    head, _, rest = text.partition("  - key: ex01/old")
    old_task, _, new_task = rest.partition("  - key: exam1/new")
    path.write_text(head + "  - key: exam1/new" + new_task + "  - key: ex01/old" + old_task)

    with pytest.raises(AdminError):
        apply_course_definition(
            database, tenant_id=TENANT, path=path, profiles_dir=PROFILES, authored_by=AUTHOR
        )

    with database.unit_of_work() as uow:
        tasks = {task.title: task for task in uow.tasks.list_for_course(_course_id(database))}
    new = tasks["新しい試験の課題"]
    assert new.confidential_until_open and new.campus_only and new.exam, "断られて保護が外れた"


def _course_id(database: Database):
    with database.unit_of_work() as uow:
        (found,) = [c for c in uow.identity.list_courses(TENANT) if c.code == "prog2"]
    return found.id
