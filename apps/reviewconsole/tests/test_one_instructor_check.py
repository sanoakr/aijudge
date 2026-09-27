"""「このコースの担当教員か」の応答を 1 か所で決める（段階的な立て直し 1-1）。

以前は同じ判定がコンソールの 4 か所に書かれていた（manage・api・app の 2 つ）。
寄せる前と**同じ応答**であることをここで固定する ── 状態コードと文言の両方。
文言は既存のテストが見ていなかったので、寄せたときに変わっても気づけなかった。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_reviewconsole import access


def _refusal(world: World, principal, **messages) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        access.require_instructor(world.console, principal, world.course.id, **messages)
    return caught.value


@pytest.mark.parametrize("role", [Role.INSTRUCTOR, Role.ADMIN])
def test_an_instructor_gets_the_course(world: World, role: Role) -> None:
    me = world.register("teacher", role)
    assert access.require_instructor(world.console, me, world.course.id).id == world.course.id
    assert access.is_instructor(world.console, me, world.course.id)


@pytest.mark.parametrize("role", [Role.ASSISTANT, Role.LEARNER])
def test_a_member_who_is_not_an_instructor_gets_403(world: World, role: Role) -> None:
    me = world.register("member", role)
    refused = _refusal(world, me)
    assert (refused.status_code, refused.detail) == (403, "この操作には担当教員の権限が必要です")
    assert not access.is_instructor(world.console, me, world.course.id)


def test_a_stranger_gets_404_as_if_there_were_no_course(world: World) -> None:
    me = world.register("stranger", None)
    refused = _refusal(world, me)
    assert (refused.status_code, refused.detail) == (404, "コースが見つかりません")
    assert not access.is_instructor(world.console, me, world.course.id)


def test_a_tenant_admin_passes_without_an_enrolment(world: World) -> None:
    me = world.register("admin", None, tenant_admin=True)
    assert access.require_instructor(world.console, me, world.course.id).id == world.course.id


def test_the_finalisation_path_keeps_its_own_words(world: World) -> None:
    """確定の経路（`app._require_course_instructor`）は提出の側から言う。"""
    words = {
        "not_found": "提出が見つかりません",
        "forbidden": "確定済みの成績を直せるのは担当教員だけです。",
    }
    assistant = _refusal(world, world.register("ta", Role.ASSISTANT), **words)
    stranger = _refusal(world, world.register("stranger", None), **words)
    assert (assistant.status_code, assistant.detail) == (403, words["forbidden"])
    assert (stranger.status_code, stranger.detail) == (404, words["not_found"])


def test_the_management_pages_answer_the_same_way(world: World) -> None:
    """画面の経路（`manage._require_instructor`）を通しても同じ応答。"""
    world.register("ta", Role.ASSISTANT)
    world.register("stranger", None)
    url = f"/manage/courses/{world.course.id}/basics"

    assistant = world.client("ta").get(url)
    stranger = world.client("stranger").get(url)

    assert (assistant.status_code, assistant.json()["detail"]) == (
        403,
        "この操作には担当教員の権限が必要です",
    )
    assert (stranger.status_code, stranger.json()["detail"]) == (404, "コースが見つかりません")
