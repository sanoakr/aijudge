"""教員だけが見る材料（評価基準・参照回答例）が、定義 → DB → 書き出しで失われない。

定義ファイルを正本にする運用（講義リポジトリとの同期）では、DB に入った値が
書き出しで落ちると、次に流した日に DB からも消える。
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from test_course_export import COURSE, PROFILES, TENANT, _database, _specs, _write_source

from aijudge_course_admin.course_definition import apply_course_definition
from aijudge_course_admin.course_export import export_course
from aijudge_course_admin.course_snapshot import snapshot_course, snapshot_definition
from aijudge_course_admin.operations import _IMPORTER, course_id_for

NOTES = "各行の直前に、ネットワーク処理としての意味が書かれているかを見る。"
ANSWER = "# サーバへ TCP で接続する\ns.connect((HOST, PORT))\n"
REFERENCE = "print(1)\n"


def _write(root: Path) -> Path:
    path = _write_source(root)
    document = copy.deepcopy(COURSE)
    first = document["tasks"][0]
    first["criteria"][0]["judging_notes"] = NOTES
    first["reference_answer"] = ANSWER
    # ディレクトリ名から位置が決まらない課題（書き出しは statement_file の形になる）
    document["tasks"].append(
        {
            "key": "ex02/comments",
            "unit": "ex02",
            "session": 2,
            "position": 5,
            "statement": "## コメントを書く ##\n\n各行にコメントを書いてください。\n",
            "reference_solution": REFERENCE,
            "reference_answer": ANSWER,
        }
    )
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), "utf-8")
    return path


def _applied(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    path = _write(source)
    database = _database(tmp_path / "a.db")
    apply_course_definition(
        database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=_IMPORTER
    )
    return database, path


def test_notes_and_answer_survive_an_export(tmp_path: Path) -> None:
    database, source = _applied(tmp_path)
    try:
        course_id = course_id_for(TENANT, "prog2", "2026-後期")
        result = export_course(database, course_id=course_id, out_dir=tmp_path / "out")
    finally:
        database.dispose()
    assert result.skipped == ()
    before, after = _specs(source), _specs(result.path)
    assert after["ex01/p1"].criteria[0].judging_notes == NOTES
    assert after["ex01/p1"].reference_answer == ANSWER
    assert after["ex02/comments"].reference_answer == ANSWER
    assert after["ex01/p1"].criteria == before["ex01/p1"].criteria


def test_a_statement_file_task_keeps_its_reference_solution(tmp_path: Path) -> None:
    """`statement_file` の形では参照解答のファイルが読まれないので、YAML に書く。"""
    database, _ = _applied(tmp_path)
    try:
        course_id = course_id_for(TENANT, "prog2", "2026-後期")
        result = export_course(database, course_id=course_id, out_dir=tmp_path / "out")
    finally:
        database.dispose()
    entry = next(
        raw
        for raw in yaml.safe_load(result.path.read_text("utf-8"))["tasks"]
        if raw.get("key") == "ex02/comments"
    )
    assert "statement_file" in entry
    assert entry["reference_solution"] == REFERENCE
    assert _specs(result.path)["ex02/comments"].reference_solution == REFERENCE


def test_the_snapshot_shows_both_on_either_side(tmp_path: Path) -> None:
    database, source = _applied(tmp_path)
    try:
        course_id = course_id_for(TENANT, "prog2", "2026-後期")
        db_side = snapshot_course(database, course_id)
    finally:
        database.dispose()
    file_side = snapshot_definition(source, tenant_id=TENANT)
    for side in (file_side, db_side):
        values = side["tasks"]["ex01/p1"]["values"]
        assert values["reference_answer"] == ANSWER
        assert values["criteria"][0]["judging_notes"] == NOTES
    assert file_side["tasks"]["ex01/p1"]["values"] == {
        name: value
        for name, value in db_side["tasks"]["ex01/p1"]["values"].items()
        if name in file_side["tasks"]["ex01/p1"]["values"]
    }
