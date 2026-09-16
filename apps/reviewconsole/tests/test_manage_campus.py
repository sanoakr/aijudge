"""学内ネットワークの設定と、問題セットの切り替え（#333）。

固定したいのは 4 つ。

見せる相手     範囲はテナント管理者だけ。切り替えはコースの教員。
自分の接続元   設定画面が「いまのあなた」を出す（書き写しの誤りに気づく唯一の手段）。
読めない行     保存の前に突き返す。黙って無視すると抜けたことに気づけない。
効いていない   範囲が未設定なら、切り替えても効かないとはっきり書く。
"""

from __future__ import annotations

from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role

CAMPUS = "133.83.80.0/24"
INSIDE = "133.83.80.110"
OUTSIDE = "82.26.195.14"


def _unit_of(world: World) -> str:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    return task.unit or "_"


def test_only_a_tenant_admin_sees_the_ranges(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("boss", Role.ADMIN, tenant_admin=True)

    assert world.client("teacher").get("/manage/campus-networks").status_code == 403
    assert world.client("boss").get("/manage/campus-networks").status_code == 200


def test_the_settings_page_shows_your_own_address(world: World) -> None:
    """**書き写しの誤りに気づく唯一の手段。**

    範囲は人が調べて入力する値で、間違いは「試験当日に全員が提出できない」
    という形でしか現れない。学内の端末でこの画面を開けば、登録すべき値が
    そのまま読める。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)

    page = (
        world.client("boss")
        .get("/manage/campus-networks", headers={"x-forwarded-for": INSIDE})
        .text
    )

    assert INSIDE in page


def test_the_page_says_whether_you_would_pass(world: World) -> None:
    world.register("boss", Role.ADMIN, tenant_admin=True)
    client = world.client("boss")
    client.post("/manage/campus-networks", data={"cidrs": CAMPUS}, follow_redirects=False)

    inside = client.get("/manage/campus-networks", headers={"x-forwarded-for": INSIDE}).text
    outside = client.get("/manage/campus-networks", headers={"x-forwarded-for": OUTSIDE}).text

    assert "学内と判定されます" in inside
    assert "学外と判定されます" in outside


def test_an_unreadable_line_is_refused_rather_than_dropped(world: World) -> None:
    """**黙って無視しない。** 抜けたことに気づかないまま試験を迎えるのが
    いちばん高くつく。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)

    response = world.client("boss").post(
        "/manage/campus-networks",
        data={"cidrs": f"{CAMPUS}\nこれは範囲ではない"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "読めない行" in response.text
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_campus_networks(world.course.tenant_id) is None


def test_saving_the_ranges_is_audited(world: World) -> None:
    """誰が提出できるかが変わる値なので、締切と同じく記録を残す。"""
    world.register("boss", Role.ADMIN, tenant_admin=True)

    world.client("boss").post(
        "/manage/campus-networks", data={"cidrs": CAMPUS}, follow_redirects=False
    )

    with world.database.unit_of_work() as uow:
        actions = [e.action.value for e in uow.audit.list_recent(world.course.tenant_id, limit=50)]
    assert "campus_networks.updated" in actions


def test_an_instructor_restricts_a_whole_unit(world: World) -> None:
    """日程と同じで、セット単位で決めて中の全課題に入る。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import = __import__("test_manage")
    task_id = _import._import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/campus-only",
        data={"campus_only": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import TaskId

        assert uow.tasks.get_task(TaskId(task_id)).campus_only is True


def test_the_unit_page_warns_when_no_range_is_configured(world: World) -> None:
    """**切り替えただけで守られていると読まれるのが、いちばん高くつく誤解。**"""
    world.register("teacher", Role.INSTRUCTOR)
    __import__("test_manage")._import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/campus-only",
        data={"campus_only": "1"},
        follow_redirects=False,
    )

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert "この制限は効いていません" in page
