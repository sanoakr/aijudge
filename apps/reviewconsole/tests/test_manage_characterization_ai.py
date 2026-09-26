"""AI を使う経路の、**いまの振る舞いを写す**（段階的な立て直し・段階 0-3b）。

0-3a（`test_manage_characterization.py`）の続き。2026-09-26 の数え直しで、テストの
参照が 0 回だったのは**コース単位の作問**（`drafts/generate`）と**シラバスの読み取り**
（`basics/read`）だった。候補・参照解答・入力の提案は正常系のテストが 1 本ずつある
ので、ここでは断る側の分岐を足す。

**モデルは呼ばない。** 担当の部品（`TaskDrafter`・`SyllabusReader`）を代役に差し替える
── 既存のテストと同じ作法。生成の中身（プロンプト・関門）は apps/admin の側で見る。
"""

from __future__ import annotations

from test_manage import World, _import_example, _seed, _use_kc
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role

SYLLABUS = (
    "本講義では C 言語の制御構造と配列を扱う。"
    "到達目標は、繰り返しを使って入力を処理できることである。"
)


def _stub_drafter(monkeypatch) -> None:
    from aijudge_authoring.drafting import DraftTestCase, TaskDraft

    class _Drafter:
        def __init__(self, *a, **kw) -> None: ...

        def draft(self, blueprint, *, key):
            from aijudge_admin.drafting import DraftResult
            from aijudge_authoring.drafting import draft_to_spec

            draft = TaskDraft(
                title="生成された課題",
                statement="## 生成 ##\n\n2 つの整数を読み、和を出力しなさい。",
                reference_solution="int main(void){return 0;}",
                test_cases=(
                    DraftTestCase(name="case1", input="1 2", expected="3"),
                    DraftTestCase(name="case2", input="2 3", expected="5"),
                ),
            )
            return DraftResult(
                spec=draft_to_spec(draft, blueprint, key=key),
                draft=draft,
                prompt_id="task_draft_ja@2",
                model="stub-model",
            )

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskDrafter", _Drafter)


# --------------------------------------------------------------------------
# コース単位の作問（`drafts/generate`）── 参照 0 回
# --------------------------------------------------------------------------


def test_generating_without_a_unit_leaves_a_draft_with_no_unit(monkeypatch, world: World) -> None:
    """**出題先は承認のときに決める**（#84）。下書きはどのセットにも入らない。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    _use_kc(world, "cs.loops.control.basic", "ループ")
    _stub_drafter(monkeypatch)

    response = client.post(
        f"/manage/courses/{world.course.id}/drafts/generate",
        data={"key_suffix": "p9", "kc": ["cs.loops.control.basic"], "readability_weight": "0.3"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    with world.database.unit_of_work() as uow:
        (draft,) = uow.tasks.list_drafts(world.course.id)
        tasks = uow.tasks.list_for_course(world.course.id)
    assert not draft.unit
    assert draft.generated_by == "stub-model"
    assert len(tasks) == 1, "下書きが課題になっている"


def test_generating_without_a_unit_needs_a_registered_component(world: World) -> None:
    """AI に KC を作らせない。問題セットからの生成と同じ規則。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/drafts/generate",
        data={"key_suffix": "p9", "kc": ["cs.made.up"]},
    )

    assert response.status_code == 400


# --------------------------------------------------------------------------
# シラバスの読み取り（`basics/read`）── 参照 0 回
# --------------------------------------------------------------------------


def _course_title(world: World) -> str:
    with world.database.unit_of_work() as uow:
        return uow.identity.get_course(world.course.id).title


def test_reading_a_syllabus_fills_the_form_without_saving(monkeypatch, world: World) -> None:
    """**読み取りと登録を分ける。** 出てきたものを教員が確かめてから保存する。"""
    from types import SimpleNamespace

    world.register("teacher", Role.INSTRUCTOR)
    before = _course_title(world)
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader.read_basics",
        lambda self, body: SimpleNamespace(title="整えた題名", markdown="## 概要\n\n整えた本文"),
    )

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/basics/read", data={"text": SYLLABUS}
    )

    assert response.status_code == 200
    assert "整えた題名" in response.text
    assert "整えた本文" in response.text
    assert "読み取りました" in response.text
    assert _course_title(world) == before, "読み取っただけで保存された"


def test_reading_falls_back_to_plain_markdown_when_the_model_is_down(
    monkeypatch, world: World
) -> None:
    """**読み取りを止めない**（P2）。モデルに繋がらなければ簡易の整形で欄に入れる。"""
    world.register("teacher", Role.INSTRUCTOR)

    def _down(self, body):
        raise ConnectionError("S6 down")

    monkeypatch.setattr("aijudge_reviewconsole.manage.SyllabusReader.read_basics", _down)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/basics/read", data={"text": SYLLABUS}
    )

    assert response.status_code == 200
    assert "簡易版" in response.text
    assert "到達目標" in response.text


def test_reading_refuses_too_little_text(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/basics/read", data={"text": "短い"}
    )

    assert response.status_code == 400


# --------------------------------------------------------------------------
# 断る側の分岐（候補・参照解答・入力の提案は正常系が既にある）
# --------------------------------------------------------------------------


def test_kc_candidates_refuse_a_statement_that_is_too_short(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/kc-candidates",
        data={"statement": "短い"},
    )

    # 空でなければ送られた問題文で出す。20 文字未満は断る。
    assert response.status_code == 400
