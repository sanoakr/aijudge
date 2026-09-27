"""コースを項目ごとの値に開く（`course snapshot`）。

**流した直後は両側が一致する**ことが前提になる。定義ファイルの書き方と
DB の持ち方の違いがここに残ると、同期する側は何もしていない項目を
「変わった」と読み、毎回 PR を起こす（`course export` を文字どおり比べて
いた頃に実際に起きた）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_course_export import PROFILES, TENANT, _database, _write_source

from aijudge_admin import course_id_for
from aijudge_admin.cli import main
from aijudge_admin.course_definition import apply_course_definition, load_course_definition
from aijudge_admin.course_snapshot import snapshot_course, snapshot_definition
from aijudge_admin.operations import _IMPORTER
from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import Task
from aijudge_persistence import Database

COURSE_ID = course_id_for(TENANT, "prog2", "2026-後期")


@pytest.fixture
def applied(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    path = _write_source(source)
    database = _database(tmp_path / "a.db")
    apply_course_definition(
        database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=_IMPORTER
    )
    yield database, path
    database.dispose()


def _differing(file_side: dict, db_side: dict) -> dict[str, set[str]]:
    """定義ファイルが書いている項目のうち、値が違うもの（課題ごと）。"""
    result: dict[str, set[str]] = {}
    for key, task in file_side["tasks"].items():
        stored = db_side["tasks"][key]["values"]
        names = {name for name, value in task["values"].items() if stored.get(name) != value}
        if names:
            result[key] = names
    return result


def test_both_sides_agree_right_after_apply(applied) -> None:
    database, path = applied
    file_side = snapshot_definition(path, tenant_id=TENANT)
    db_side = snapshot_course(database, COURSE_ID)

    assert file_side["course_id"] == db_side["course_id"] == str(COURSE_ID)
    assert set(file_side["tasks"]) == set(db_side["tasks"])
    assert _differing(file_side, db_side) == {}
    # コースの値も、書いたものは揃っている。
    for name, value in file_side["course"].items():
        if name != "knowledge_components":
            assert db_side["course"][name] == value


def test_times_are_compared_as_instants_not_as_text(applied) -> None:
    """定義ファイルの `+09:00` と DB の UTC が、同じ時刻なら同じ値になる。"""
    _, path = applied
    file_side = snapshot_definition(path, tenant_id=TENANT)
    due = file_side["tasks"]["ex01/p1"]["values"]["task.due_at"]
    assert due == "2026-10-01T03:00:00Z"


def test_unwritten_schedule_is_not_managed_by_the_file(applied) -> None:
    """書いていない日程は出さない（`course apply` が既存の値を残す項目）。"""
    database, path = applied
    file_side = snapshot_definition(path, tenant_id=TENANT)
    assert "task.grading_starts_at" not in file_side["tasks"]["ex01/p1"]["values"]
    assert "unit.confidential_until_open" not in file_side["tasks"]["ex01/p1"]["values"]
    # DB 側は全部出す（同期する側が、定義ファイルの書いた項目だけを見る）。
    assert (
        "task.grading_starts_at"
        in snapshot_course(database, COURSE_ID)["tasks"]["ex01/p1"]["values"]
    )


def test_a_schedule_changed_in_the_console_is_the_only_difference(applied) -> None:
    database, path = applied
    edited_at = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    with database.unit_of_work() as uow:
        task = next(t for t in uow.tasks.list_for_course(COURSE_ID) if t.unit == "ex01")
        due = task.due_at + timedelta(days=7)
        uow.tasks.save_task(Task.model_validate(task.model_dump() | {"due_at": due}))
        # コンソールの `_update_unit` と同じ形で残す。
        AuditRecorder.for_system(uow.audit, tenant_id=TENANT, clock=lambda: edited_at).record(
            AuditAction.TASK_UPDATED,
            target_type="unit",
            target_id=f"{COURSE_ID}/ex01",
            summary="問題セットの日程を変えた",
            detail={"course_id": str(COURSE_ID), "changed": {"due_at": {}}},
        )
        uow.commit()

    db_side = snapshot_course(database, COURSE_ID)
    differing = _differing(snapshot_definition(path, tenant_id=TENANT), db_side)
    assert differing == {"ex01/p1": {"task.due_at"}}
    assert db_side["tasks"]["ex01/p1"]["changed_at"]["task.due_at"] == "2026-09-27T03:00:00Z"
    # 監査記録に無い項目は時刻が分からない（`null`）。
    assert db_side["tasks"]["ex01/p1"]["changed_at"]["task.title"] is None


def test_a_revised_statement_carries_the_time_of_its_version(applied, tmp_path: Path) -> None:
    database, path = applied
    before = snapshot_course(database, COURSE_ID)["tasks"]["ex02/p1"]["changed_at"]["statement"]

    desc = path.parent / "ex02" / "p1" / "desc.md"
    desc.write_text(desc.read_text(encoding="utf-8") + "\n負の数も来ます。\n", encoding="utf-8")
    apply_course_definition(
        database,
        path,
        tenant_id=TENANT,
        profiles_dir=PROFILES,
        authored_by=_IMPORTER,
        revise=True,
    )

    db_side = snapshot_course(database, COURSE_ID)
    changed_at = db_side["tasks"]["ex02/p1"]["changed_at"]
    assert changed_at["statement"] >= before
    # 変えていない項目は、最初の版の時刻のまま。
    assert changed_at["test_cases"] == before
    assert _differing(snapshot_definition(path, tenant_id=TENANT), db_side) == {}


def test_the_database_side_carries_a_spec_to_write_back(applied) -> None:
    """DB から定義ファイルへ書き戻すときに使う宣言（`course export` と同じもの）。"""
    database, path = applied
    db_side = snapshot_course(database, COURSE_ID)
    specs = {spec.key: spec for spec in load_course_definition(path).tasks}
    for key, task in db_side["tasks"].items():
        assert task["spec"]["key"] == key
        assert task["spec"]["statement"] == specs[key].statement


def test_the_cli_prints_both_sides_as_json(applied, capsys) -> None:
    """同期する側（運用機の cron）はこの JSON を読む。"""
    database, path = applied
    url = str(database.engine.url)
    tenant = ["--tenant", str(TENANT)]

    assert main([*tenant, "course", "snapshot", "--file", str(path)]) == 0
    file_side = json.loads(capsys.readouterr().out)
    assert (
        main(["--database-url", url, *tenant, "course", "snapshot", "--course", str(COURSE_ID)])
        == 0
    )
    db_side = json.loads(capsys.readouterr().out)

    assert (file_side["source"], db_side["source"]) == ("file", "db")
    assert _differing(file_side, db_side) == {}


def test_the_cli_refuses_an_unknown_course(tmp_path: Path, capsys) -> None:
    url = f"sqlite+pysqlite:///{tmp_path}/empty.db"
    Database.connect(url, create=True).dispose()
    assert main(["--database-url", url, "course", "snapshot", "--course", "crs_missing"]) == 2
    assert "コースがありません" in capsys.readouterr().err


def test_an_unwritten_position_is_not_managed_by_the_file(tmp_path: Path) -> None:
    """位置を書かない課題は、DB が位置を振っても差にならない（#484）。"""
    import yaml
    from test_course_export import COURSE

    source = tmp_path / "source"
    source.mkdir()
    path = _write_source(source)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        task.pop("position", None)
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    assert COURSE["tasks"][0].get("position") is not None  # 元の定義は位置を書いている

    database = _database(tmp_path / "a.db")
    try:
        apply_course_definition(
            database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=_IMPORTER
        )
        file_side = snapshot_definition(path, tenant_id=TENANT)
        db_side = snapshot_course(database, COURSE_ID)
    finally:
        database.dispose()
    assert "task.position" not in file_side["tasks"]["ex02/p2"]["values"]
    assert db_side["tasks"]["ex02/p2"]["values"]["task.position"] is not None
    assert _differing(file_side, db_side) == {}
