"""テストケースの終了コードの期待（2026-10-18）が、定義 → DB → 書き出しで失われない。

定義ファイルを正本にする運用（講義リポジトリとの同期）では、書き出しで落ちた値は
次に流した日に DB からも消える。`in/` `out/` には置き場が無いので、期待を持つ
ケースは YAML に書かれなければならない。
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from test_course_export import COURSE, PROFILES, TENANT, _database, _specs, _write_source

from aijudge_course_admin.course_definition import apply_course_definition
from aijudge_course_admin.course_export import export_course
from aijudge_course_admin.operations import _IMPORTER, course_id_for


def _write(root: Path) -> Path:
    path = _write_source(root)
    document = copy.deepcopy(COURSE)
    document["tasks"].append(
        {
            "key": "ex02/double",
            "unit": "ex02",
            "session": 2,
            "position": 6,
            "statement": "## 2 倍 ##\n\n正の整数なら 2 倍を出力する。それ以外は何も出力しない。\n",
            "test_cases": [
                {"name": "case1", "input": "4\n", "expected": "8\n"},
                {"name": "case2", "input": "-1\n", "expected": "", "exit_code": "nonzero"},
            ],
        }
    )
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), "utf-8")
    return path


def test_the_exit_expectation_survives_an_export(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = _write(source)
    database = _database(tmp_path / "a.db")
    try:
        apply_course_definition(
            database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=_IMPORTER
        )
        course_id = course_id_for(TENANT, "prog2", "2026-後期")
        result = export_course(database, course_id=course_id, out_dir=tmp_path / "out")
    finally:
        database.dispose()

    assert result.skipped == ()
    before, after = _specs(path), _specs(result.path)
    assert after["ex02/double"].test_cases == before["ex02/double"].test_cases
    assert [case.exit_code for case in after["ex02/double"].test_cases] == ["zero", "nonzero"]
    # `in/` `out/` に書き戻さない（置き場が無く、期待が消える）。
    assert not (result.path / "ex02" / "double" / "in").exists()
