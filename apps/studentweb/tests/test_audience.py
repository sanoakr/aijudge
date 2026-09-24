"""出題先を名簿で絞る（追試など・`Task.audience_group_ids`）。

固定したいのは 5 つ。

外の人には無い   出題先の外の学習者には、一覧にも URL にも出ず、出せない。
中の人には出る   名簿に入っている学習者には、いつもどおり出る。
未提出と数えない  外の人の一覧で「未提出」に数えない（対象外と未提出は別の状態）。
自分の提出は残る  名簿から外れても、既に出した提出は見られる。
TA は名簿に無関係 TA は名簿に入っていなくても見える（質問対応のため）。
"""

from __future__ import annotations

from test_studentweb import COURSE, EXAMPLE_SOURCE, TENANT, World, _set_task
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import CourseGroup, Role, new_id
from aijudge_core.ids import CourseGroupId

EMPTY_LIST = "公開されている課題がありません"


def _retake(world: World, *members) -> CourseGroupId:
    """「追試」グループを作り、課題の出題先にする。"""
    group = CourseGroup(
        id=CourseGroupId(new_id("grp")), tenant_id=TENANT, course_id=COURSE, name="追試"
    )
    with world.database.unit_of_work() as uow:
        uow.identity.save_group(group)
        uow.identity.set_group_members(group.id, frozenset(m.user_id for m in members))
        uow.commit()
    _set_task(world, audience_group_ids=(group.id,))
    return group.id


def _submit(world: World):
    return world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", EXAMPLE_SOURCE.read_bytes(), "text/plain")},
        follow_redirects=False,
    )


def test_a_learner_outside_the_audience_sees_nothing(world: World) -> None:
    insider = world.register("s2400001")
    world.register("s2400002")
    _retake(world, insider)
    world.login("s2400002")

    assert EMPTY_LIST in world.client.get(f"/courses/{COURSE}").text
    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 404
    assert _submit(world).status_code == 404


def test_a_learner_inside_the_audience_sees_it(world: World) -> None:
    insider = world.register("s2400001")
    _retake(world, insider)
    world.login("s2400001")

    assert EMPTY_LIST not in world.client.get(f"/courses/{COURSE}").text
    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 200
    assert _submit(world).status_code == 303


def test_it_is_not_counted_as_unsubmitted_for_someone_outside(world: World) -> None:
    """**対象外と未提出は別の状態。** 混ぜると、追試を受けない学生全員が
    「追試を出していない」と読める。"""
    insider = world.register("s2400001")
    world.register("s2400002")
    _retake(world, insider)
    world.login("s2400002")
    assert "未提出" not in world.client.get(f"/courses/{COURSE}").text

    # 同じ画面が、名簿の中の学習者には「未提出」と言う（上の否定が空振りでない）。
    world.login("s2400001")
    assert "未提出" in world.client.get(f"/courses/{COURSE}").text


def test_a_learner_removed_from_the_group_keeps_their_submission(world: World) -> None:
    insider = world.register("s2400001")
    group_id = _retake(world, insider)
    world.login("s2400001")
    submitted = _submit(world).headers["location"]

    with world.database.unit_of_work() as uow:
        uow.identity.set_group_members(group_id, frozenset())
        uow.commit()

    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 404
    assert world.client.get(submitted).status_code == 200, "自分の提出が見えなくなった"


def test_an_assistant_sees_it_without_being_in_the_group(world: World) -> None:
    insider = world.register("s2400001")
    world.register("ta", role=Role.ASSISTANT)
    _retake(world, insider)
    world.login("ta")

    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 200
