"""採点の一覧と 1 件の画面を、採点権限の無い人が開けないこと（#706）。

`test_console.py` の `test_a_learner_cannot_open_the_queue` が叩くのはコースの
メニューだけで、一覧（再確認の依頼・提出・確定処理・人が読む）はそれぞれ別の
検査（`_queue_rows`・`_blind_rows`）を通る。1 件の画面も、拒否を見ていたのは
reveal の GET だけで、提出物の配信と blind・確定の POST は担当教員の成功側しか
叩かれていなかった。ここでは 3 つの立場で叩く。

学習者            このコースの受講者（提出した本人）
受講の無い利用者   どのコースにも登録が無い
他コースの教員     別のコースの担当教員（このコースには何の受講も無い）

どれも「無い」と答える（404）── 存在を伝えると、提出 ID やコースの存在が漏れる。

あわせて、**状態を変える要求が別オリジンから来たら断る**ミドルウェア（#413）が
教員コンソールに載っていることを固定する。判定そのものは
`packages/identity/tests/test_same_origin_only.py` が確かめているが、コンソールに
配線されていることはどのテストも見ていなかった。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_console import (
    COURSE,
    PASSWORD,
    PROFILE,
    TENANT,
    World,
    _agree_form,
    needs_c_compiler,
)
from test_console import blind_world as blind_world  # フィクスチャを借りる
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Course, Role
from aijudge_core.ids import CourseId
from aijudge_identity import AuthService

OTHER_COURSE = CourseId("crs_" + "2" * 32)
#: 採点の一覧。`/blind` と `/manual` は「人が読む」（`/read`）への転送なので、
#: 転送先まで追って確かめる。
COURSE_LISTS = ("queue", "submissions", "finalize", "read", "blind", "manual")
OUTSIDERS = ("learner", "stranger", "other_instructor")
#: 別のオリジン。同じサイトの別ホストからの POST を想定する（SameSite=Lax では止まらない）。
FOREIGN_ORIGIN = "https://other.example"


def _outsiders(world: World) -> dict[str, str]:
    """立場ごとの login。学習者はこのコースの受講者として登録する。"""
    world.register("s2400001", role=Role.LEARNER)
    with world.database.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=OTHER_COURSE,
                tenant_id=TENANT,
                code="net",
                title="ネットワーク及び演習",
                term="2026-前期",
                subject_profile=PROFILE,
            )
        )
        service = AuthService(uow.identity, audit=uow.audit)
        service.register(tenant_id=TENANT, login="stranger", display_name="s", password=PASSWORD)
        other = service.register(
            tenant_id=TENANT, login="other_teacher", display_name="o", password=PASSWORD
        )
        service.enroll(
            tenant_id=TENANT, course_id=OTHER_COURSE, user_id=other.user_id, role=Role.INSTRUCTOR
        )
        uow.commit()
    return {"learner": "s2400001", "stranger": "stranger", "other_instructor": "other_teacher"}


def _learner(world: World):
    """`_outsiders` が登録した学習者（`world.submit` に渡せる形）。"""
    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "s2400001")
    return SimpleNamespace(user_id=user.id)


# --------------------------------------------------------------------------
# 一覧
# --------------------------------------------------------------------------


@pytest.mark.parametrize("who", OUTSIDERS)
@pytest.mark.parametrize("page", COURSE_LISTS)
def test_the_grading_lists_are_not_found_for_outsiders(world: World, page: str, who: str) -> None:
    """一覧には他の学習者の再確認の依頼とその理由・提出・点数が並ぶ。"""
    logins = _outsiders(world)
    world.login(logins[who])

    response = world.client.get(f"/courses/{COURSE}/{page}")

    assert response.status_code == 404, response.text


def test_the_grading_lists_open_for_a_grader(world: World) -> None:
    """上の 404 が URL の誤りでないことの対照。TA なら同じ URL が開く。"""
    world.register("ta", role=Role.ASSISTANT)
    world.login("ta")
    for page in COURSE_LISTS:
        assert world.client.get(f"/courses/{COURSE}/{page}").status_code == 200, page


# --------------------------------------------------------------------------
# 1 件の画面（提出物・blind・確定）
# --------------------------------------------------------------------------


def _graded_submission(world: World):
    logins = _outsiders(world)
    accepted = world.submit(_learner(world))
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    return logins, accepted.submission, run


def _human_records(world: World, submission, run):
    with world.database.unit_of_work() as uow:
        return (
            uow.reviews.find_blind_mark(submission.id),
            uow.reviews.find_review_for_run(run.id),
            uow.reviews.find_finalization_for_run(run.id),
        )


@needs_c_compiler
@pytest.mark.parametrize("who", OUTSIDERS)
def test_a_submission_is_not_found_for_outsiders(blind_world: World, who: str) -> None:
    """**提出物の配信・blind 採点・確定のどれも 404 で、何も記録されない。**

    blind の抽出に当たる世界（`blind_world`）で叩くので、検査が無ければ blind
    採点がそのまま保存される。確定は AI の判定に同意する正しいフォームで送る。
    """
    logins, submission, run = _graded_submission(blind_world)
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    artifact = submission.artifacts[0]
    blind_world.login(logins[who])

    served = blind_world.client.get(f"/review/{submission.id}/artifacts/{artifact.id}")
    blind = blind_world.client.post(
        f"/review/{submission.id}/blind", data=_agree_form(blind_world, machine)
    )
    finalized = blind_world.client.post(
        f"/review/{submission.id}/finalize", data=_agree_form(blind_world, machine)
    )

    assert (served.status_code, blind.status_code, finalized.status_code) == (404, 404, 404)
    assert artifact.filename not in served.text
    assert _human_records(blind_world, submission, run) == (None, None, None)


@needs_c_compiler
def test_an_artifact_of_another_submission_is_not_served(world: World) -> None:
    """**提出物は、その提出に属するものだけを配る。** 担当教員でも、URL の提出と
    別の提出の `artifact_id` を組み合わせると 404。ID だけで引く書き換えを止める。"""
    _outsiders(world)
    learner = _learner(world)
    first = world.submit(learner).submission
    second = world.submit(learner, payload=b"int main(void){return 0;}\n").submission
    # 採点の無い提出は 1 件の画面が 409 で断るので、先に採点を済ませる。
    world.worker.run_until_empty()
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    foreign = second.artifacts[0].id

    mixed = world.client.get(f"/review/{first.id}/artifacts/{foreign}")
    own = world.client.get(f"/review/{second.id}/artifacts/{foreign}")

    assert mixed.status_code == 404
    assert own.status_code == 200, "対照: 正しい組み合わせなら配る"


# --------------------------------------------------------------------------
# 別オリジンからの POST（#413）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_cross_origin_finalize_is_refused(world: World) -> None:
    """ログイン済みの教員の Cookie が付いていても、`Origin` が別ホストなら 403。"""
    _logins, submission, run = _graded_submission(world)
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    url = f"/review/{submission.id}/finalize"

    foreign = world.client.post(
        url,
        data=_agree_form(world, machine),
        headers={"Origin": FOREIGN_ORIGIN},
        follow_redirects=False,
    )
    assert foreign.status_code == 403
    assert _human_records(world, submission, run)[1:] == (None, None)

    # 対照: 同じオリジンからなら同じフォームで確定できる。
    same = world.client.post(
        url,
        data=_agree_form(world, machine),
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert same.status_code == 303, same.text


def test_a_cross_origin_manage_save_is_refused(world: World) -> None:
    """`/manage` の保存も同じミドルウェアの後ろにある。コースは変わらない。"""
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    with world.database.unit_of_work() as uow:
        before = uow.identity.get_course(COURSE)

    response = world.client.post(
        f"/manage/courses/{COURSE}/upload-formats",
        data={"formats": "pdf"},
        headers={"Origin": FOREIGN_ORIGIN},
        follow_redirects=False,
    )

    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(COURSE) == before
