"""出題先の名簿を外から登録する API（`docs/design/task-visibility.md` §3.5）。

画面が無くても、手元のスクリプトから名簿を作り、問題セットに割り当てられる
こと。将来グループごとに問題セットを出し分けるときの入口になる。

固定したいのは 5 つ。

冪等       同じ PUT を 2 度流しても、2 度目は何も足さず何も外さない。
全部か無   知らない login が 1 つでもあれば 422 で、何も保存しない。
割り当て   問題セットの出題先を名前で置き換え、空に戻せる。学生画面に効く。
消せない   出題先として使われているグループの DELETE は 409。
権限       トークンでしか通らず、TA のトークンでは 403（課題の API と同じ）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_api import SPEC, World

from aijudge_core import Role


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    for login in ("s2400001", "s2400002"):
        w.user(login, Role.LEARNER)
    yield w
    w.database.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _put(world: World, token: str, members: list[str], name: str = "追試"):
    return world.client.put(
        f"/api/courses/{world.course.id}/groups/{name}",
        json={"members": members},
        headers=_auth(token),
    )


def _with_a_task(world: World, token: str) -> None:
    response = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=_auth(token)
    )
    assert response.status_code == 201, response.text


def test_putting_the_same_roster_twice_is_idempotent(world: World) -> None:
    token = world.token("teacher")

    first = _put(world, token, ["s2400001", "s2400002"])
    second = _put(world, token, ["s2400001", "s2400002"])

    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    assert first.json()["added"] == ["s2400001", "s2400002"]
    assert second.json() | {"members": None} == {
        "name": "追試",
        "created": False,
        "added": [],
        "removed": [],
        "members": None,
    }
    listed = world.client.get(f"/api/courses/{world.course.id}/groups", headers=_auth(token))
    assert listed.json() == [{"name": "追試", "members": 2, "used_by": 0}]


def test_one_unknown_login_stores_nothing(world: World) -> None:
    token = world.token("teacher")

    response = _put(world, token, ["s2400001", "s2499999"])

    assert response.status_code == 422
    assert response.json()["detail"]["unknown_logins"] == ["s2499999"]
    fetched = world.client.get(f"/api/courses/{world.course.id}/groups/追試", headers=_auth(token))
    assert fetched.status_code == 404


def test_assigning_a_set_reaches_the_learner_view(world: World) -> None:
    """API で割り当てた出題先が、そのまま `may_see` に効く。"""
    token = world.token("teacher")
    _with_a_task(world, token)
    _put(world, token, ["s2400001"])

    response = world.client.put(
        f"/api/courses/{world.course.id}/units/ex02/audience",
        json={"groups": ["追試"]},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"unit": "ex02", "tasks": 1, "groups": ["追試"]}
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        group = uow.identity.find_group(world.course.id, "追試")
    assert task.audience_group_ids == (group.id,)

    cleared = world.client.put(
        f"/api/courses/{world.course.id}/units/ex02/audience",
        json={"groups": []},
        headers=_auth(token),
    )
    assert cleared.status_code == 200
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.audience_group_ids == ()


def test_an_unknown_group_or_unit_is_404(world: World) -> None:
    token = world.token("teacher")
    _with_a_task(world, token)

    unknown_group = world.client.put(
        f"/api/courses/{world.course.id}/units/ex02/audience",
        json={"groups": ["無い"]},
        headers=_auth(token),
    )
    unknown_unit = world.client.put(
        f"/api/courses/{world.course.id}/units/nowhere/audience",
        json={"groups": []},
        headers=_auth(token),
    )

    assert unknown_group.status_code == 404
    assert unknown_unit.status_code == 404


def test_a_group_in_use_cannot_be_deleted(world: World) -> None:
    token = world.token("teacher")
    _with_a_task(world, token)
    _put(world, token, ["s2400001"])
    world.client.put(
        f"/api/courses/{world.course.id}/units/ex02/audience",
        json={"groups": ["追試"]},
        headers=_auth(token),
    )

    in_use = world.client.delete(
        f"/api/courses/{world.course.id}/groups/追試", headers=_auth(token)
    )
    assert in_use.status_code == 409

    world.client.put(
        f"/api/courses/{world.course.id}/units/ex02/audience",
        json={"groups": []},
        headers=_auth(token),
    )
    freed = world.client.delete(f"/api/courses/{world.course.id}/groups/追試", headers=_auth(token))
    assert freed.status_code == 204


def test_an_assistant_token_is_refused(world: World) -> None:
    token = world.token("ta", Role.ASSISTANT)

    assert _put(world, token, ["s2400001"]).status_code == 403
    assert (
        world.client.get(f"/api/courses/{world.course.id}/groups", headers=_auth(token)).status_code
        == 403
    )


def test_without_a_token_nothing_passes(world: World) -> None:
    response = world.client.put(
        f"/api/courses/{world.course.id}/groups/追試", json={"members": ["s2400001"]}
    )

    assert response.status_code == 401
