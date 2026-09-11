"""コースを消す CLI の入口（#156）。

削除の規則そのものは `aijudge_admin.courses` にあり、画面（`/manage`）と
共有している。ここで確かめるのは**入口の側**である ── 確認を挟むか、
テナントをまたがないか、規模を先に出すか。

**規則の再実装をここで許すと、画面と CLI で答えが割れる。** だからこの
ファイルは「提出があれば消せない」ことを CLI からも観測できると示すに
とどめ、境界の判断そのものは `tests/test_deleting_a_course_leaves_nothing_behind.py`
に置いたままにする。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin.cli import main
from aijudge_admin.operations import ensure_course
from aijudge_core.ids import TenantId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/cli.db"


@pytest.fixture
def course(db_url: str):
    database = Database.connect(db_url, create=True)
    try:
        course, _ = ensure_course(
            database,
            tenant_id=TENANT,
            code="test_c",
            title="テスト科目",
            term="2026-後期",
            subject_profile="cs_lang_c_intro",
            profiles_dir=PROFILES,
        )
    finally:
        database.dispose()
    return course


def _cli(db_url: str, tmp_path: Path, *args: str, tenant: str = str(TENANT)) -> int:
    return main(
        [
            "--database-url",
            db_url,
            "--tenant",
            tenant,
            "--profiles",
            str(PROFILES),
            "--artifacts",
            str(tmp_path / "artifacts"),
            *args,
        ]
    )


def test_the_cli_deletes_a_course_that_nobody_submitted_to(
    db_url: str, tmp_path: Path, course, capsys
) -> None:
    assert _cli(db_url, tmp_path, "course", "delete", "--course", str(course.id), "--yes") == 0
    out = capsys.readouterr().out
    assert "消しました" in out
    # **規模を先に出す。** 空振りと本当の削除が同じ顔で終わってはいけない。
    assert "学習者の提出    0 件" in out

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            assert uow.identity.get_course(course.id) is None
    finally:
        database.dispose()


def test_the_cli_does_not_delete_another_tenants_course(
    db_url: str, tmp_path: Path, course, capsys
) -> None:
    """**打ち間違いが他機関のコースに届く経路にしない。**"""
    assert (
        _cli(
            db_url,
            tmp_path,
            "course",
            "delete",
            "--course",
            str(course.id),
            "--yes",
            tenant=str(OTHER_TENANT),
        )
        == 2
    )
    assert "このテナントのコースではありません" in capsys.readouterr().err


def test_an_unknown_course_is_reported_not_crashed(db_url: str, tmp_path: Path, course, capsys):
    assert _cli(db_url, tmp_path, "course", "delete", "--course", "crs_" + "f" * 32, "--yes") == 2
    assert "がありません" in capsys.readouterr().err


def test_without_yes_the_cli_asks_first(
    db_url: str, tmp_path: Path, course, monkeypatch, capsys
) -> None:
    """**既定では確認を挟む。** 答えなければ何も消えない。"""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert _cli(db_url, tmp_path, "course", "delete", "--course", str(course.id)) == 1
    assert "中止しました" in capsys.readouterr().out

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            assert uow.identity.get_course(course.id) is not None
    finally:
        database.dispose()
