"""出題先の名簿を CLI から流す入口（`aijudge-admin group` / `unit audience`）。

規則そのもの（一部だけ登録しない・学習者だけ・使用中は消せない）は
`aijudge_admin.groups` にあり、画面と API と共有している（`test_groups.py`）。
ここで確かめるのは**入口の側** ── ファイルの読み方、何が起きたかを出すこと、
失敗を終了コードで返すこと、監査の操作者を偽らないこと。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin.cli import main
from aijudge_admin.operations import ensure_course
from aijudge_core import Role, Task, new_id
from aijudge_core.ids import TaskId, TenantId
from aijudge_identity import AuthService
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)


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
            code="prog2",
            title="プログラミング演習 II",
            term="2026-後期",
            subject_profile="cs_lang_c_intro",
            profiles_dir=PROFILES,
        )
        with database.unit_of_work() as uow:
            service = AuthService(uow.identity, audit=uow.audit)
            for login in ("s2400001", "s2400002"):
                user = service.register(
                    tenant_id=TENANT, login=login, display_name=login, password="p" * 12
                )
                service.enroll(
                    tenant_id=TENANT, course_id=course.id, user_id=user.user_id, role=Role.LEARNER
                )
            uow.tasks.save_task(
                Task(id=TaskId(new_id("tsk")), course_id=course.id, title="追試1", unit="retake")
            )
            uow.commit()
    finally:
        database.dispose()
    return course


def _cli(db_url: str, tmp_path: Path, *args: str) -> int:
    return main(
        [
            "--database-url",
            db_url,
            "--tenant",
            str(TENANT),
            "--profiles",
            str(PROFILES),
            "--artifacts",
            str(tmp_path / "artifacts"),
            *args,
        ]
    )


def _roster(tmp_path: Path, text: str) -> str:
    path = tmp_path / "roster.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_setting_a_roster_and_assigning_a_unit(db_url: str, tmp_path: Path, course, capsys) -> None:
    roster = _roster(tmp_path, "# 前期追試\ns2400001  山田\n\ns2400002 # 再履修\n")

    assert (
        _cli(
            db_url,
            tmp_path,
            "group",
            "set",
            "--course",
            str(course.id),
            "--name",
            "追試",
            "--members",
            roster,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "作った（2 名）" in out
    assert "追加: s2400001" in out

    assert (
        _cli(
            db_url,
            tmp_path,
            "unit",
            "audience",
            "--course",
            str(course.id),
            "--unit",
            "retake",
            "--group",
            "追試",
        )
        == 0
    )
    assert "1 課題の出題先を 追試 にした" in capsys.readouterr().out

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            (task,) = uow.tasks.list_for_course(course.id)
            group = uow.identity.find_group(course.id, "追試")
            actors = {
                e.actor_kind.value
                for e in uow.audit.list_recent(TENANT, limit=20)
                if e.action.value in ("group.updated", "task.updated")
            }
    finally:
        database.dispose()
    assert task.audience_group_ids == (group.id,)
    # CLI は認証された主体を持たないので、操作者は `system` と正直に残す。
    assert actors == {"system"}


def test_an_unknown_login_fails_with_a_nonzero_exit(
    db_url: str, tmp_path: Path, course, capsys
) -> None:
    roster = _roster(tmp_path, "s2400001\ns2499999\n")

    code = _cli(
        db_url,
        tmp_path,
        "group",
        "set",
        "--course",
        str(course.id),
        "--name",
        "追試",
        "--members",
        roster,
    )

    assert code == 1
    assert "s2499999" in capsys.readouterr().err


def test_clearing_the_audience_without_a_group(db_url: str, tmp_path: Path, course, capsys) -> None:
    roster = _roster(tmp_path, "s2400001\n")
    _cli(
        db_url,
        tmp_path,
        "group",
        "set",
        "--course",
        str(course.id),
        "--name",
        "追試",
        "--members",
        roster,
    )
    _cli(
        db_url,
        tmp_path,
        "unit",
        "audience",
        "--course",
        str(course.id),
        "--unit",
        "retake",
        "--group",
        "追試",
    )

    assert (
        _cli(db_url, tmp_path, "unit", "audience", "--course", str(course.id), "--unit", "retake")
        == 0
    )
    assert "受講者全員" in capsys.readouterr().out
    assert (
        _cli(db_url, tmp_path, "group", "delete", "--course", str(course.id), "--name", "追試") == 0
    )
