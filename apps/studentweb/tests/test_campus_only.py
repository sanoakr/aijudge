"""学内限定の課題（#333）。

固定したいのは 5 つ。

断るのは経路   画面で隠すだけでは、URL を知っていれば出せる。
理由を分ける   「学外から」と「判定できない」は学習者にとって意味が違う。
未設定は通す   範囲を決める前に制限を入れても、誰も締め出されない。
偽れない       `X-Forwarded-For` の左端を名乗っても通らない（右端で判定する）。
先に言う       出そうとする前に、いまの接続元でどうなるかが画面に出る。
"""

from __future__ import annotations

import pytest
from test_studentweb import COURSE, TENANT, World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TenantId
from aijudge_identity import CampusNetworkSettings

CAMPUS = ("133.83.80.0/24",)
INSIDE = "133.83.80.110"
OUTSIDE = "82.26.195.14"


def _restrict(world: World, *, campus_only: bool = True, cidrs=CAMPUS) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update={"campus_only": campus_only}))
        uow.identity.save_campus_networks(
            CampusNetworkSettings(tenant_id=TenantId(TENANT), cidrs=tuple(cidrs))
        )
        uow.commit()


def _submit_from(world: World, ip: str | None):
    headers = {"x-forwarded-for": ip} if ip else {}
    return world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", b"int main(void){return 0;}\n", "text/plain")},
        headers=headers,
        follow_redirects=False,
    )


def test_a_submission_from_campus_is_accepted(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    assert _submit_from(world, INSIDE).status_code == 303


def test_a_submission_from_outside_is_refused_at_the_route(world: World) -> None:
    """**画面で隠すだけでは制限にならない。** URL を知っていれば出せてしまう。"""
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    response = _submit_from(world, OUTSIDE)

    assert response.status_code == 409
    assert "学内からのみ" in response.text


def test_the_two_refusals_read_differently(world: World) -> None:
    """場所を移せば直るのか、移しても直らないのかが分かること。"""
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    outside = _submit_from(world, OUTSIDE).text
    unknown = _submit_from(world, "nonsense").text

    assert "学外" in outside
    assert "判定できません" in unknown
    assert outside != unknown


def test_the_left_of_the_forwarded_header_cannot_be_claimed(world: World) -> None:
    """**右端で判定する。** 左端はクライアントが自由に書ける。

    左端を見る実装だと、学外から `X-Forwarded-For: 133.83.80.1` と名乗る
    だけで通れる。
    """
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    # 左端に学内、右端（逆プロキシが書く側）に学外。
    response = _submit_from(world, f"{INSIDE}, {OUTSIDE}")

    assert response.status_code == 409, "左端を名乗るだけで通っている"


def test_without_any_range_nobody_is_locked_out(world: World) -> None:
    """**範囲を決める前に制限を入れても、誰も締め出されない。**

    締め出す作りにすると、設定を忘れた瞬間に試験が止まる。
    """
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world, cidrs=())

    assert _submit_from(world, OUTSIDE).status_code == 303


def test_an_unrestricted_task_ignores_the_source(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world, campus_only=False)

    assert _submit_from(world, OUTSIDE).status_code == 303


def test_the_task_page_says_so_before_you_try(world: World) -> None:
    """**出そうとして断られる前に言う。** 試験の最中に場所を移すことになる。"""
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    outside = world.client.get(
        f"/tasks/{world.task_version.id}", headers={"x-forwarded-for": OUTSIDE}
    ).text
    inside = world.client.get(
        f"/tasks/{world.task_version.id}", headers={"x-forwarded-for": INSIDE}
    ).text

    assert "学外から接続しています" in outside
    assert "学内から接続しています" in inside


def test_the_course_list_marks_the_restricted_task(world: World) -> None:
    """一覧にも出す ── 開くまで分からないと、学外で何が出せるか読めない。"""
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    page = world.client.get(f"/courses/{COURSE}").text

    assert "学内のみ" in page


@pytest.mark.parametrize("role", [Role.INSTRUCTOR, Role.ASSISTANT])
def test_staff_submit_from_outside(world: World, role: Role) -> None:
    """**教員・TA は学内限定を通れる**（2026-09-25）。提出は動作確認で成績に数えない。
    画面も「提出できません」とは言わない。"""
    world.register("staff", role=role)
    world.login("staff")
    _restrict(world)

    assert _submit_from(world, OUTSIDE).status_code == 303
    page = world.client.get(
        f"/tasks/{world.task_version.id}", headers={"x-forwarded-for": OUTSIDE}
    ).text
    assert "教員・TA は学外からも提出できます" in page
    assert "提出できません。" not in page


def test_the_exemption_does_not_reach_learners(world: World) -> None:
    """同じ課題でも、学習者は学外から断られたまま。"""
    world.register("staff", role=Role.INSTRUCTOR)
    world.register("s2400001")
    world.login("s2400001")
    _restrict(world)

    assert _submit_from(world, OUTSIDE).status_code == 409
    page = world.client.get(
        f"/tasks/{world.task_version.id}", headers={"x-forwarded-for": OUTSIDE}
    ).text
    assert "教員・TA は学外からも" not in page
