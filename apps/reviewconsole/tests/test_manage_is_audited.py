"""`/manage` の操作が監査ログに残ること（ADR 0016）。

**画面を通して確かめる。** 監査行を書く場所は関数の中だが、そこに正しい
操作者と対象が届いているかは、ハンドラを実際に叩かないと分からない
（`me` の取り違え、テナントの取り違えは単体では見えない）。

ここで押さえるのは、権限・資格情報・成績の設定という
**「成績に届く操作」の 3 系統**である。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# テストディレクトリに `__init__.py` は無い（CLAUDE.md の規約）。pytest が
# そのディレクトリを sys.path に足すので、素の import で引ける。
from test_manage import PASSWORD, TENANT, World

from aijudge_audit import ActorKind, AuditAction
from aijudge_core import Role
from aijudge_core.ids import TenantId


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


def rows(world: World, action: AuditAction | None = None):
    with world.database.unit_of_work() as uow:
        return uow.audit.list_recent(TenantId(TENANT), action=action)


def test_granting_tenant_admin_is_recorded_with_both_values(world: World) -> None:
    """**前後の値を書く。** 「変えた」だけでは、いま管理者なのがこの操作の
    結果なのか元からなのか、後から読めない。
    """
    admin = world.register("boss", None, tenant_admin=True)
    target = world.register("teacher", Role.INSTRUCTOR)
    client = world.client("boss")

    response = client.post(
        f"/manage/users/{target.user_id}/tenant-admin", data={"admin": "1"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text

    (row,) = rows(world, AuditAction.TENANT_ADMIN_CHANGED)
    assert row.actor_user_id == admin.user_id
    assert row.actor_role == "tenant_admin"
    assert row.target_id == str(target.user_id)
    assert row.detail["is_tenant_admin"] == {"before": False, "after": True}
    # 運用ログとの結び目（ADR 0016）。
    assert row.request_id is not None


def test_disabling_a_user_is_recorded(world: World) -> None:
    world.register("boss", None, tenant_admin=True)
    target = world.register("student", Role.LEARNER)
    client = world.client("boss")

    assert (
        client.post(f"/manage/users/{target.user_id}/disable", follow_redirects=False).status_code
        == 303
    )

    (row,) = rows(world, AuditAction.USER_DISABLED)
    assert row.target_id == str(target.user_id)
    assert row.detail["login"] == "student"


def test_a_reissued_password_is_recorded_but_never_stored(world: World) -> None:
    """**平文は記録しない。**

    画面に一度だけ出すためのもので（#144）、消さない場所に写せば
    「一度だけ」が嘘になる。
    """
    world.register("boss", None, tenant_admin=True)
    target = world.register("student", Role.LEARNER)
    client = world.client("boss")

    response = client.post(f"/manage/users/{target.user_id}/password")
    assert response.status_code == 200, response.text

    (row,) = rows(world, AuditAction.PASSWORD_REISSUED)
    assert row.target_id == str(target.user_id)
    # 画面に出た平文が監査行のどこにも無いこと。
    shown = response.text
    assert all(
        token not in repr(row.detail)
        for token in shown.split()
        if len(token) > 12 and token.isascii()
    )
    assert "password" not in repr(row.detail)


def test_changing_the_grace_period_is_recorded_with_both_values(world: World) -> None:
    """自動確定の猶予は**成績がいつ閉じるかを決める値**（ADR 0014）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/auto-finalize",
        data={"after_minutes": "1440"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    (row,) = rows(world, AuditAction.COURSE_UPDATED)
    assert row.target_id == str(world.course.id)
    assert row.detail["auto_finalize_after_minutes"] == {"before": None, "after": 1440}


def test_a_login_failure_is_recorded_even_though_nothing_was_saved(world: World) -> None:
    """**失敗こそ残す。**

    監査行は操作と同じトランザクションに載るので、失敗のハンドラが
    そのまま return すると記録ごと巻き戻る。総当たりに気づけなくなる。
    """
    user = world.register("student", Role.LEARNER)
    client = world.client("student")

    response = client.post(
        "/auth/local",
        data={"login": "student", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 401

    (row,) = rows(world, AuditAction.LOGIN_FAILED)
    # 口座は分かっているが、**やった人は分からない**。
    assert row.actor_kind is ActorKind.ANONYMOUS
    assert row.actor_user_id is None
    assert row.target_id == str(user.user_id)
    assert row.detail["reason"] == "bad password"


def test_a_successful_login_names_the_user(world: World) -> None:
    user = world.register("student", Role.LEARNER)
    world.client("student")

    (row,) = rows(world, AuditAction.LOGIN_SUCCEEDED)
    assert row.actor_kind is ActorKind.USER
    assert row.actor_user_id == user.user_id


def test_the_password_never_reaches_the_audit_log(world: World) -> None:
    """ログイン失敗の記録に、試されたパスワードを入れない。

    入れると、**打ち間違えた本物のパスワードが消さない場所に残る**。
    残すのは打たれた ID までで足りる（誰の口座が狙われたかは分かる）。
    """
    world.register("student", Role.LEARNER)
    client = world.client("student")
    client.post(
        "/auth/local",
        data={"login": "student", "password": PASSWORD + "-typo"},
        follow_redirects=False,
    )

    for row in rows(world, AuditAction.LOGIN_FAILED):
        assert PASSWORD not in repr(row.detail)
        assert "-typo" not in repr(row.detail)
