"""観点のモデルの重さ（`judge_model`）を画面から扱う（2026-11-13）。

**コンソールで保存しても名指しが消えない**ことを押さえる。2026-09 の ex01-2 では
コンソールの保存が検証データを壊した。欄の無い項目は、観点を保存するたびに黙って
落ちる ── ここで落ちると、次の版から既定の軽いモデルで採点される。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from test_manage import PROFILES, World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskVersionId
from aijudge_course_admin import rubric

PROFILE = "cs_lang_c_intro"


@pytest.fixture
def heavy_world(world: World, tmp_path: Path) -> World:
    """科目プロファイルに重さ `large` を足した世界。"""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    for path in PROFILES.glob("*.yaml"):
        shutil.copy(path, profiles / path.name)
    data = yaml.safe_load((profiles / f"{PROFILE}.yaml").read_text("utf-8"))
    data["judge_models"] = {"large": {"model": "qwen3.6:35b-a3b"}}
    (profiles / f"{PROFILE}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8"
    )
    world.console.profiles_dir = profiles
    return world


def _task(world: World):
    """読みやすさの観点に `judge_model: large` を持つ課題を作る。"""
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
            criterion.model_copy(update={"judge_model": "large"})
            if criterion.code == "readability"
            else criterion
            for criterion in first.criteria
        )
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "e" * 32),
                    "version": first.version + 1,
                    "criteria": criteria,
                }
            )
        )
        uow.commit()
    return client, task


def _latest(world: World, task):
    with world.database.unit_of_work() as uow:
        return uow.tasks.latest_version(task.id)


def _models(version) -> dict[str, str | None]:
    return {criterion.code: criterion.judge_model for criterion in version.criteria}


def _form(version, statement: str, **overrides: str) -> dict[str, list[str]]:
    """編集画面が送る観点の欄を、いまの観点から組み立てる（ブラウザと同じ形）。"""
    form: dict[str, list[str]] = {"statement": [statement]}
    for row in rubric.to_rows(version.criteria):
        # 上書きは読みやすさの観点（重さを名指している方）にだけ当てる
        values = {**row, **(overrides if row["code"] == "readability" else {})}
        for field in (
            "code",
            "title",
            "description",
            "weight",
            "order",
            "evaluator",
            "levels",
            "judging_notes",
            "judge_model",
        ):
            form.setdefault(f"criterion_{field}", []).append(str(values.get(field) or ""))
        form.setdefault("criterion_original", []).append(str(row["code"]))
    return form


def test_the_task_page_offers_the_profile_names(heavy_world: World) -> None:
    client, task = _task(heavy_world)
    page = client.get(f"/manage/courses/{heavy_world.course.id}/tasks/{task.id}/edit").text
    assert 'name="criterion_judge_model"' in page
    assert '<option value="large" selected>large</option>' in page
    assert "科目の既定のモデル" in page


def test_saving_the_criteria_keeps_the_name(heavy_world: World) -> None:
    client, task = _task(heavy_world)
    before = _latest(heavy_world, task)
    response = client.post(
        f"/manage/courses/{heavy_world.course.id}/tasks/{task.id}/revise",
        data=_form(before, "## [必須] コメントを書く ##\n\n誤字を直しました。"),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    latest = _latest(heavy_world, task)
    assert latest.version == before.version + 1
    assert _models(latest)["readability"] == "large"


def test_fixing_only_the_statement_keeps_the_name(heavy_world: World) -> None:
    """観点の欄を送らない訂正でも、いまの観点ごと引き継ぐ。"""
    client, task = _task(heavy_world)
    response = client.post(
        f"/manage/courses/{heavy_world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] コメントを書く ##\n\n誤字を直しました。",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert _models(_latest(heavy_world, task))["readability"] == "large"


def test_a_name_the_profile_does_not_declare_is_refused(heavy_world: World) -> None:
    client, task = _task(heavy_world)
    before = _latest(heavy_world, task)
    response = client.post(
        f"/manage/courses/{heavy_world.course.id}/tasks/{task.id}/revise",
        data=_form(before, before.statement, judge_model="huge"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "judge_models" in response.text
    assert _latest(heavy_world, task).version == before.version


def test_a_model_id_typed_into_the_field_is_refused_in_words(heavy_world: World) -> None:
    client, task = _task(heavy_world)
    before = _latest(heavy_world, task)
    response = client.post(
        f"/manage/courses/{heavy_world.course.id}/tasks/{task.id}/revise",
        data=_form(before, before.statement, judge_model="qwen3.6:35b-a3b"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "モデルの重さ" in response.text


def test_clearing_the_field_returns_to_the_default_model(heavy_world: World) -> None:
    client, task = _task(heavy_world)
    before = _latest(heavy_world, task)
    response = client.post(
        f"/manage/courses/{heavy_world.course.id}/tasks/{task.id}/revise",
        data=_form(before, before.statement, judge_model=""),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert _models(_latest(heavy_world, task))["readability"] is None
