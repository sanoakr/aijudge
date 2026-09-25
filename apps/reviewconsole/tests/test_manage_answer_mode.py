"""問題セットの答え方の切り替え（ADR 0026）。

固定したいのは 3 つ。

セット単位     学内限定と同じく、中の全課題に同じ値が入る。
保存時の検査   テストを走らせない課題は `editor` にできない。画面で押せなく
               するだけでは境界にならない（#146）ので、保存する側が断る。
既定           何もしなければ従来どおりファイル提出（不変条件 I4・I5）。
"""

from __future__ import annotations

import pytest
from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_admin.answer_mode import editor_blockers
from aijudge_core import AnswerMode, Role
from aijudge_core.ids import TaskId


def _pair(world: World):
    from aijudge_admin import list_tasks

    return list_tasks(world.database, world.course.id)[0]


def _task(world: World, task_id: str):
    with world.database.unit_of_work() as uow:
        return uow.tasks.get_task(TaskId(task_id))


def test_a_task_answers_by_upload_unless_told_otherwise(world: World) -> None:
    task_id = _import_example(world)
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


def test_an_instructor_puts_a_whole_unit_in_the_editor(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"file_upload": "1", "editor": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _task(world, task_id).answer_mode is AnswerMode.EDITOR
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "答え方" in page

    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"file_upload": "1"},
        follow_redirects=False,
    )
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


def test_switching_is_audited(world: World) -> None:
    """学習者の画面が変わる操作なので、誰がいつ変えたかを残す。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"file_upload": "1", "editor": "1"},
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        events = uow.audit.list_recent(world.course.tenant_id, limit=20)
    assert any(e.detail.get("field") == "answer_mode" for e in events)


def test_the_server_refuses_the_editor_for_a_task_that_runs_no_tests(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**保存する側が断る。** 画面は押せなくしてあるが、それは境界ではない。"""
    import aijudge_reviewconsole.manage as manage

    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    monkeypatch.setattr(
        manage, "editor_blockers", lambda *a, **k: ("tsk_x: テストを走らせない課題です",)
    )

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"file_upload": "1", "editor": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "テストを走らせない" in response.text
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


def test_turning_both_off_is_refused(world: World) -> None:
    """**両方外すと誰も提出できない。** 保存する側が断り、課題は変わらない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _task(world, task_id).file_upload is True


def test_a_unit_can_be_editor_only(world: World) -> None:
    """**エディタだけ**（試験）。ファイルの提出は止まり、画面のチェックも外れて見える。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"editor": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    task = _task(world, task_id)
    assert task.answer_mode is AnswerMode.EDITOR
    assert task.file_upload is False
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    start = page.index('name="file_upload"')
    assert "checked" not in page[start : page.index(">", start)]


def test_the_editor_box_is_greyed_out_when_a_task_cannot_use_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """画像だけの課題などを含むセットでは、エディタのチェックを押せなくする（理由も出す）。"""
    import aijudge_reviewconsole.manage as manage

    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    monkeypatch.setattr(
        manage,
        "editor_blockers",
        lambda *a, **k: ("認定証: 提出形式に .c・.py・.md のどれも含まれていません",),
    )

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/{unit}").text
    editor_box = page[page.index('name="editor"') : page.index(">", page.index('name="editor"'))]
    assert "disabled" in editor_box
    assert "認定証" in page


def test_an_assistant_cannot_switch(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"file_upload": "1", "editor": "1"},
        follow_redirects=False,
    )

    assert response.status_code in (403, 404)
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


# -- 検査そのもの ------------------------------------------------------------


def test_the_rule_accepts_a_task_that_takes_code(world: World) -> None:
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": (".c",)})
    assert editor_blockers([(task, version)], world.course) == ()


def test_the_rule_accepts_a_report_written_as_text(world: World) -> None:
    """オンラインのレポート試験。`.md` だけの課題もエディタにできる。"""
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": (".md",)})
    assert editor_blockers([(task, version)], world.course) == ()


def test_a_set_with_nothing_the_editor_writes_is_blocked(world: World) -> None:
    """画像・PDF だけのセットはエディタにしない（2026-09-25）。"""
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": (".pdf", ".jpg")})

    reasons = editor_blockers([(task, version)], world.course)

    assert len(reasons) == 1
    assert "エディタで書ける課題がありません" in reasons[0]


def test_a_mixed_set_can_use_the_editor(world: World) -> None:
    """**書ける課題が 1 つあれば、画像の課題が混ざっていてもエディタにできる**（2026-09-25）。
    画像・PDF はエディタの画面からファイルを選んで出す。"""
    task, version = _pair(world_with_example(world))
    image_only = task.model_copy(update={"accepted_suffixes": (".png", ".pdf")})

    assert editor_blockers([(task, version), (image_only, version)], world.course) == ()


def test_a_video_task_keeps_file_upload(world: World) -> None:
    """**動画を受ける課題があれば、ファイル選択は外せない**。動画はエディタから出せない。"""
    from aijudge_admin.answer_mode import file_upload_required

    task, version = _pair(world_with_example(world))
    video = task.model_copy(update={"accepted_suffixes": (".mp4",), "title": "実演の動画"})

    assert file_upload_required([(task, version)], world.course) == ()
    reasons = file_upload_required([(task, version), (video, version)], world.course)
    assert len(reasons) == 1 and "実演の動画" in reasons[0]


def test_the_rule_falls_back_to_the_courses_formats(world: World) -> None:
    """課題が形式を指定しなければコースの既定（受付と同じ `allowed_suffixes`）。"""
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": ()})
    pdf_only = world.course.model_copy(update={"upload_suffixes": (".pdf",)})

    assert editor_blockers([(task, version)], pdf_only) != ()
    assert editor_blockers([(task, version)], world.course) == ()


def world_with_example(world: World) -> World:
    _import_example(world)
    return world


def test_completion_is_switched_for_the_whole_set(world: World) -> None:
    """補完は答え方とは独立した値で、セット単位で入る（設計書 §5.3）。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    assert _task(world, task_id).editor_completion is False

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/completion",
        data={"completion": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _task(world, task_id).editor_completion is True
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD  # 答え方は変わらない
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "エディタで補完を出す" in page
    assert "この設定は効きません" in page


# -- クリア点（2026-09-25）------------------------------------------------------


def test_an_instructor_sets_the_clear_points_of_a_set(world: World) -> None:
    """クリア点は問題セットの値で、中の全課題に入る。空欄に戻せば条件なし。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/clear-points",
        data={"clear_points": "60"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _task(world, task_id).clear_points == 60.0
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "クリア条件" in page and 'value="60"' in page

    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/clear-points",
        data={"clear_points": ""},
        follow_redirects=False,
    )
    assert _task(world, task_id).clear_points is None


def test_a_non_positive_clear_points_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/clear-points",
        data={"clear_points": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert _task(world, task_id).clear_points is None


def test_the_server_refuses_editor_only_for_a_set_with_video(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**動画を受ける課題があるセットは、ファイル選択を外せない**（保存する側が断る）。"""
    import aijudge_reviewconsole.manage as manage

    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    monkeypatch.setattr(
        manage, "file_upload_required", lambda *a, **k: ("実演: 動画はエディタから出せません",)
    )

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"editor": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert _task(world, task_id).file_upload is True
    # 両方入れるのは通る（動画は課題の画面から出す）。
    ok = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"editor": "1", "file_upload": "1"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


# -- 採点のされ方の印（2026-09-25）------------------------------------------------


def test_the_grading_method_is_named_by_evaluator(world: World) -> None:
    """「自動テストなし」だけだと、AI が判定する課題（感想・レポート）まで自動採点されない
    ように読めた。観点の担当から「テスト・規則・AI・教員」を出す。"""
    from aijudge_core import HUMAN_SCORED
    from aijudge_reviewconsole.manage import _graded_by

    _task_obj, version = _pair(world_with_example(world))
    base = version.criteria[0]

    def with_evaluators(*names):
        weight = 1.0 / len(names)
        criteria = tuple(
            base.model_copy(update={"code": f"c{i}", "evaluator_id": name, "weight": weight})
            for i, name in enumerate(names)
        )
        return version.model_copy(update={"criteria": criteria})

    assert _graded_by(with_evaluators(None)) == ("AI が判定",)
    assert _graded_by(with_evaluators("code_test_runner", None)) == ("テストで判定", "AI が判定")
    assert _graded_by(with_evaluators("text_pattern_check")) == ("規則で判定",)
    assert _graded_by(with_evaluators(HUMAN_SCORED)) == ("教員が採点",)


# -- 学生の画面への入口（2026-09-25）---------------------------------------------


def test_the_console_links_to_the_learner_view_of_the_course(world: World) -> None:
    """コンソールの上の帯に「学生の画面」。コースの中ならそのコースの学生の画面へ。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert "学生の画面" in page
    assert f'/courses/{world.course.id}" target="_blank"' in page


# -- 問題セットの一覧を、いま誰に見えているかで分ける（2026-09-25）---------------


def _set_opening(world: World, task_id: str, *, days: int, confidential: bool = False) -> None:
    from datetime import UTC, datetime, timedelta

    when = datetime.now(UTC) + timedelta(days=days)
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        uow.tasks.save_task(
            task.model_copy(update={"opens_at": when, "confidential_until_open": confidential})
        )
        uow.commit()


def test_the_set_list_is_split_by_who_can_see_it(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    client = world.client("teacher")
    menu = f"/courses/{world.course.id}"

    assert "学生に公開中" in client.get(menu).text

    _set_opening(world, task_id, days=1)
    page = client.get(menu).text
    assert "未公開（TA まで見える）" in page and "学生に公開中" not in page

    _set_opening(world, task_id, days=1, confidential=True)
    assert "教員のみ（TA にも未公開）" in client.get(menu).text

    _set_opening(world, task_id, days=-1, confidential=True)
    assert "学生に公開中" in client.get(menu).text
