"""教員だけが見る材料（評価基準・参照回答例）を画面から扱う（2026-09-28）。

観点の説明は提出後の結果画面に出る。回答例を説明に書くと答えが見えるので、
AI にだけ渡す欄を分けた。**画面は欄ごとに学生に見えるかを言い**、訂正の
どの経路でもその欄を黙って落とさない。
"""

from __future__ import annotations

from pathlib import Path

from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskVersionId
from aijudge_course_admin import rubric

NOTES = "各行の直前に、ネットワーク処理としての意味が書かれているかを見る。"
ANSWER = "# サーバへ TCP で接続する\ns.connect((HOST, PORT))"
STUDENT_TEMPLATES = (
    Path(__file__).resolve().parents[2] / "studentweb" / "src" / "aijudge_studentweb" / "templates"
)


def _task(world: World):
    """観点に評価基準、課題に参照回答例を持つ課題を作る。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p9",
            "unit": "ex09",
            "statement": "## [必須] コメントを書く ##\n\n各行にコメントを書いてください。",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)
        criteria = tuple(
            criterion.model_copy(update={"judging_notes": NOTES})
            if criterion.code == "readability"
            else criterion
            for criterion in first.criteria
        )
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "d" * 32),
                    "version": first.version + 1,
                    "criteria": criteria,
                    "reference_answer": ANSWER,
                }
            )
        )
        uow.commit()
    return client, task


def _latest(world: World, task):
    with world.database.unit_of_work() as uow:
        return uow.tasks.latest_version(task.id)


def _notes(version) -> dict[str, str | None]:
    return {criterion.code: criterion.judging_notes for criterion in version.criteria}


def test_the_task_page_says_who_sees_each_field(world: World) -> None:
    client, task = _task(world)
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert 'name="criterion_judging_notes"' in page
    assert 'name="reference_answer"' in page
    assert "提出後、学生の結果画面に出ます" in page
    assert "学生には見えません" in page
    assert "AI 評価器にだけ渡します" in page
    # 保存済みの値が欄に入っている（HTML では > がエスケープされないので原文で探す）
    assert NOTES in page
    assert "s.connect((HOST, PORT))" in page


def test_fixing_the_statement_keeps_notes_and_answer(world: World) -> None:
    """問題文だけ直す訂正（欄の無いフォーム）でも、黙って消さない。"""
    client, task = _task(world)
    before = _latest(world, task)
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] コメントを書く ##\n\n誤字を直しました。",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    latest = _latest(world, task)
    assert latest.version == before.version + 1
    assert _notes(latest)["readability"] == NOTES
    assert latest.reference_answer == ANSWER


def test_the_answer_field_can_change_and_clear_the_answer(world: World) -> None:
    client, task = _task(world)
    url = f"/manage/courses/{world.course.id}/tasks/{task.id}/revise"
    statement = "## [必須] コメントを書く ##\n\n各行にコメントを書いてください。"

    client.post(url, data={"statement": statement, "reference_answer": "# 別の例\r\nx = 1"})
    assert _latest(world, task).reference_answer == "# 別の例\nx = 1"

    client.post(url, data={"statement": statement, "reference_answer": "  "})
    assert _latest(world, task).reference_answer is None


def test_the_rubric_rows_carry_the_notes_both_ways() -> None:
    """画面の行 ⇄ 観点で評価基準が往復する（空欄は「無し」）。"""
    rows = [
        {
            "code": "comments",
            "title": "コメント",
            "description": "各行の意味がネットワークの言葉で書かれているか。",
            "weight": "1.0",
            "levels": "未達 | 無い | 0\n達成 | ある | 1",
            "judging_notes": f"  {NOTES}  ",
        },
    ]
    (spec,) = rubric.parse(rows)
    assert spec.judging_notes == NOTES
    assert rubric.to_rows([spec])[0]["judging_notes"] == NOTES
    (blank,) = rubric.parse([{**rows[0], "judging_notes": " "}])
    assert blank.judging_notes is None


def test_student_pages_never_read_the_teacher_only_fields() -> None:
    """学生の画面は評価基準も参照回答例も出さない（テンプレートが名指ししない）。"""
    assert STUDENT_TEMPLATES.is_dir()
    for template in STUDENT_TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "judging_notes" not in text, template.name
        assert "reference_answer" not in text, template.name
