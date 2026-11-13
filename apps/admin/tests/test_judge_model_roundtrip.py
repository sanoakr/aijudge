"""観点のモデルの重さ（`judge_model`）が、定義 → DB → 書き出し → スナップショットで失われない。

2026-11-13、gemma4:e4b ではレポートやコメント課題が重すぎたので、観点ごとに重い
モデルを名指せるようにした。名指しは定義ファイル（lecture-courses）に書かれ、
course-sync が DB と往復させる ── どこかで落ちると、次に流した日に黙って既定の
軽いモデルへ戻る（コンソール保存が宣言を壊した ex01-2 と同じ形）。
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml
from test_course_export import COURSE, PROFILES, TENANT, _database, _specs, _write_source

from aijudge_course_admin.course_definition import apply_course_definition
from aijudge_course_admin.course_export import export_course
from aijudge_course_admin.course_snapshot import snapshot_course, snapshot_definition
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.operations import _IMPORTER, course_id_for

PROFILE = "cs_lang_c_intro"
JUDGE_MODELS = {"large": {"model": "qwen3.6:35b-a3b", "fallback_model": "qwen3.8:27b-mlx"}}


def _profiles(root: Path, *, with_large: bool) -> Path:
    """見本のプロファイルを写し、重さの対応を足す（足さない版も作れる）。"""
    target = root / "profiles"
    target.mkdir()
    for path in PROFILES.glob("*.yaml"):
        shutil.copy(path, target / path.name)
    if with_large:
        data = yaml.safe_load((target / f"{PROFILE}.yaml").read_text("utf-8"))
        data["judge_models"] = JUDGE_MODELS
        (target / f"{PROFILE}.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8"
        )
    return target


def _write(root: Path) -> Path:
    path = _write_source(root)
    document = copy.deepcopy(COURSE)
    criterion = document["tasks"][0]["criteria"][0]
    # 人が採点する観点にモデルは要らないので、AI が判定する観点にしてから名指す
    criterion.pop("evaluator")
    criterion["judge_model"] = "large"
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), "utf-8")
    return path


def _applied(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    path = _write(source)
    database = _database(tmp_path / "a.db")
    apply_course_definition(
        database,
        path,
        tenant_id=TENANT,
        profiles_dir=_profiles(tmp_path, with_large=True),
        authored_by=_IMPORTER,
    )
    return database, path


def _first_criterion(document: dict) -> dict:
    return document["tasks"][0]["criteria"][0]


def test_the_definition_carries_the_name_into_the_version(tmp_path: Path) -> None:
    database, _ = _applied(tmp_path)
    try:
        with database.unit_of_work() as uow:
            course_id = course_id_for(TENANT, "prog2", "2026-後期")
            versions = [
                uow.tasks.latest_version(t.id) for t in uow.tasks.list_for_course(course_id)
            ]
    finally:
        database.dispose()
    named = [c for v in versions if v for c in v.criteria if c.judge_model]
    assert [c.judge_model for c in named] == ["large"]


def test_the_name_survives_an_export(tmp_path: Path) -> None:
    database, source = _applied(tmp_path)
    try:
        course_id = course_id_for(TENANT, "prog2", "2026-後期")
        result = export_course(database, course_id=course_id, out_dir=tmp_path / "out")
    finally:
        database.dispose()
    assert result.skipped == ()
    before, after = _specs(source), _specs(result.path)
    key = next(key for key, spec in before.items() if spec.criteria)
    assert after[key].criteria[0].judge_model == "large"
    assert after[key].criteria == before[key].criteria


def test_the_snapshot_shows_the_name_on_either_side(tmp_path: Path) -> None:
    """course-sync はスナップショットで両側を比べる。片側で落ちると毎回「違う」になる。"""
    database, source = _applied(tmp_path)
    try:
        course_id = course_id_for(TENANT, "prog2", "2026-後期")
        db_side = snapshot_course(database, course_id)
    finally:
        database.dispose()
    file_side = snapshot_definition(source, tenant_id=TENANT)
    key = next(
        key
        for key, entry in file_side["tasks"].items()
        if entry["values"].get("criteria")
        and entry["values"]["criteria"][0].get("judge_model") == "large"
    )
    assert db_side["tasks"][key]["values"]["criteria"][0]["judge_model"] == "large"


def test_a_name_the_profile_does_not_declare_is_refused_before_saving(tmp_path: Path) -> None:
    """採点のたびに失敗する観点を入れない。課題を 1 件も保存する前に断る。"""
    source = tmp_path / "source"
    source.mkdir()
    path = _write(source)
    database = _database(tmp_path / "a.db")
    try:
        with pytest.raises(AdminError, match="judge_models"):
            apply_course_definition(
                database,
                path,
                tenant_id=TENANT,
                profiles_dir=_profiles(tmp_path, with_large=False),
                authored_by=_IMPORTER,
            )
        with database.unit_of_work() as uow:
            course_id = course_id_for(TENANT, "prog2", "2026-後期")
            assert list(uow.tasks.list_for_course(course_id)) == []
    finally:
        database.dispose()
