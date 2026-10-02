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


def _register_range(world: World, text: str = f"{CAMPUS}  # 1 号館 101 教室・有線") -> None:
    world.register("boss", Role.ADMIN, tenant_admin=True)
    world.client("boss").post(
        "/manage/campus-networks", data={"cidrs": text}, follow_redirects=False
    )


def test_an_instructor_restricts_a_whole_unit(world: World) -> None:
    """日程と同じで、セット単位で決めて中の全課題に入る。"""
    _register_range(world)
    world.register("teacher", Role.INSTRUCTOR)
    _import = __import__("test_manage")
    task_id = _import._import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/campus-only",
        data={"campus_only": "1", "campus_ranges": [CAMPUS]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import TaskId

        saved = uow.tasks.get_task(TaskId(task_id))
    assert saved.campus_only is True
    assert saved.campus_ranges == (CAMPUS,)


def test_restricting_a_unit_requires_choosing_a_range(world: World) -> None:
    """**学内限定にするなら、どこから受け付けるかを決める。** 選ばずに保存できない。"""
    _register_range(world)
    world.register("teacher", Role.INSTRUCTOR)
    __import__("test_manage")._import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/campus-only",
        data={"campus_only": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "1 つ以上選んでください" in response.text


def test_an_unregistered_range_cannot_be_chosen(world: World) -> None:
    _register_range(world)
    world.register("teacher", Role.INSTRUCTOR)
    __import__("test_manage")._import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/campus-only",
        data={"campus_only": "1", "campus_ranges": ["10.0.0.0/8"]},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_the_notes_are_saved_and_shown_when_choosing(world: World) -> None:
    """管理者が書いた注釈（どの教室・どの回線か）が、教員の選ぶ画面に出る。"""
    _register_range(world, f"{CAMPUS}  # 1 号館 101 教室・有線\n133.83.82.0/25  # 2 号館・無線 LAN")
    world.register("teacher", Role.INSTRUCTOR)
    __import__("test_manage")._import_example(world)
    unit = _unit_of(world)

    admin_page = world.client("boss").get("/manage/campus-networks").text
    page = (
        world.client("teacher")
        .get(f"/manage/courses/{world.course.id}/units/{unit}", headers={"x-forwarded-for": INSIDE})
        .text
    )

    assert "1 号館 101 教室・有線" in admin_page
    assert "1 号館 101 教室・有線" in page and "2 号館・無線 LAN" in page
    assert "いまの接続元はここ" in page


def test_a_note_longer_than_the_limit_is_refused(world: World) -> None:
    world.register("boss", Role.ADMIN, tenant_admin=True)

    response = world.client("boss").post(
        "/manage/campus-networks",
        data={"cidrs": f"{CAMPUS}  # " + "あ" * 121},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_the_unit_page_warns_when_no_range_is_configured(world: World) -> None:
    """**切り替えただけで守られていると読まれるのが、いちばん高くつく誤解。**"""
    world.register("teacher", Role.INSTRUCTOR)
    __import__("test_manage")._import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    # 範囲が未登録のあいだは、選ぶ範囲が無いので保存できない。**既存のセット**
    # （範囲を選べる前に学内限定にしたもの）の見え方を固定するため、課題に直接入れる。
    with world.database.unit_of_work() as uow:
        for task in uow.tasks.list_for_course(world.course.id):
            uow.tasks.save_task(task.model_copy(update={"campus_only": True}))
        uow.commit()

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert "この制限は効いていません" in page
