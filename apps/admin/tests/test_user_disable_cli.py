"""利用者を止める経路が CLI にあること（#237）。

**作るのは CLI だけ**（`staff` と名簿の取り込み。画面に平文パスワードを
出さないため）なのに、止めるのは `/manage/users` だけだった ── そちらは
テナント管理者専用で、**サーバに入れる人はその権限の外側にいる**。
作った本人が片付けられない状態で、#210 で直したコース削除と同じ形である。

規則そのもの（削除ではなく無効化・過去の提出が参照している）は
`AuthService.disable` にあり、ここでは入口だけを確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin.cli import main
from aijudge_core import Role
from aijudge_core.ids import TenantId
from aijudge_identity import UserState
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/users.db"


def _cli(db_url: str, *args: str) -> int:
    return main(
        [
            "--database-url",
            db_url,
            "--tenant",
            str(TENANT),
            "--profiles",
            str(PROFILES),
            "--create-schema",
            *args,
        ]
    )


def _a_user(db_url: str, login: str = "sensei") -> None:
    assert (
        _cli(
            db_url,
            "staff",
            "--login",
            login,
            "--role",
            Role.INSTRUCTOR.value,
            "--password",
            "test-password",
        )
        == 0
    )


def _state(db_url: str, login: str) -> UserState:
    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            user = uow.identity.find_user_by_login(TENANT, login)
            assert user is not None
            return user.state
    finally:
        database.dispose()


def test_the_cli_disables_a_user(db_url: str, capsys) -> None:
    _a_user(db_url)
    assert _cli(db_url, "user", "disable", "--login", "sensei") == 0
    assert "無効化しました" in capsys.readouterr().out
    assert _state(db_url, "sensei") is UserState.DISABLED


def test_disabling_is_not_deleting(db_url: str) -> None:
    """**行は残る。** 過去の提出と採点が参照している。"""
    _a_user(db_url)
    _cli(db_url, "user", "disable", "--login", "sensei")

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            assert uow.identity.find_user_by_login(TENANT, "sensei") is not None
    finally:
        database.dispose()


def test_an_unknown_login_is_reported_not_crashed(db_url: str, capsys) -> None:
    _a_user(db_url)
    assert _cli(db_url, "user", "disable", "--login", "nobody") == 2
    assert "がありません" in capsys.readouterr().err


def test_disabling_twice_says_so(db_url: str, capsys) -> None:
    """**空振りと本当の無効化を同じ顔で終わらせない。**"""
    _a_user(db_url)
    _cli(db_url, "user", "disable", "--login", "sensei")
    assert _cli(db_url, "user", "disable", "--login", "sensei") == 2
    assert "既に無効" in capsys.readouterr().err


def test_the_disabling_is_recorded(db_url: str) -> None:
    """誰が成績に届く何を変えたかを残す（ADR 0016）。CLI は `system`。"""
    _a_user(db_url)
    _cli(db_url, "user", "disable", "--login", "sensei")

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            user = uow.identity.find_user_by_login(TENANT, "sensei")
            assert user is not None
            rows = uow.audit.list_for_target("user", str(user.id))
    finally:
        database.dispose()
    assert any(row.action.value == "user.disabled" for row in rows)
