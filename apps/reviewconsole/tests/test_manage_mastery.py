"""習熟度の画面（#328）。

固定したいのは 4 つ。

見せる相手      教員とテナント管理者だけ。TA には開けない。
母数を偽らない  記録のある受講者と受講者を別に出し、空を 0% と書かない。
記録の始まり    推移がいつからの記録かを言う（空の図を「動いていない」と読ませない）。
コースで割る    根拠はこのコースの提出だけを開き、外は件数だけ。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import (
    MasteryModel,
    Role,
    SkillEvidence,
    SkillPoint,
    SkillState,
)
from aijudge_core.ids import CriterionScoreId, GradingRunId, KcId, UserId

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def _kc(world: World, key: str, label: str) -> KcId:
    from aijudge_admin import register_kc

    return register_kc(world.database, key=key, label=label, namespaces=("cs",), seeding=True).id


def _scope(world: World, keys: tuple[str, ...]) -> None:
    """このコースが使う知識要素を決める（画面の「名前空間から足す」と同じ）。"""
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        uow.identity.save_course(course.model_copy(update={"knowledge_components": keys}))
        uow.commit()


def _record(
    world: World,
    learner: UserId,
    kc: KcId,
    mastery: float,
    *,
    evidence: tuple[SkillEvidence, ...] = (),
    at: datetime = NOW,
) -> None:
    with world.database.unit_of_work() as uow:
        uow.skills.save_state(
            SkillState(
                tenant_id=world.course.tenant_id,
                learner_id=learner,
                kc_id=kc,
                mastery=mastery,
                model=MasteryModel.BKT,
                observation_count=max(1, len(evidence)),
                evidence=evidence,
                updated_at=at,
            )
        )
        uow.skills.append_point(
            SkillPoint(
                tenant_id=world.course.tenant_id,
                learner_id=learner,
                kc_id=kc,
                mastery=mastery,
                observation_count=1,
                model=MasteryModel.BKT,
                recorded_at=at,
            )
        )
        uow.commit()


def test_a_ta_cannot_open_the_mastery_screen(world: World) -> None:
    """**TA には開けない。** 採点は分担するが、習熟度は成績から積み上がる値で、
    締切や受講の変更と同じ側にある（`_require_enrolment_manager`）。
    """
    world.register("ta", Role.ASSISTANT)

    assert world.client("ta").get(f"/manage/courses/{world.course.id}/mastery").status_code == 403


def test_an_instructor_and_an_admin_can_open_it(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("boss", Role.ADMIN, tenant_admin=True)

    for login in ("teacher", "boss"):
        assert (
            world.client(login).get(f"/manage/courses/{world.course.id}/mastery").status_code == 200
        )


def test_an_empty_course_says_so_rather_than_drawing_zero(world: World) -> None:
    """**空を 0% と書かない。** 図だけ出すと「全員できていない」と読まれる。"""
    world.register("teacher", Role.INSTRUCTOR)
    _scope(world, ("cs.loops",))
    _kc(world, "cs.loops", "くりかえし")

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/mastery").text

    assert "まだ記録がありません" in page
    assert "0%" not in page.split("まだ記録がありません")[0]


def test_the_screen_states_both_denominators(world: World) -> None:
    """記録のある受講者と受講者は別。**両方出す。**"""
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s1", Role.LEARNER).user_id
    world.register("s2", Role.LEARNER)
    world.register("s3", Role.LEARNER)
    kc = _kc(world, "cs.loops", "くりかえし")
    _scope(world, ("cs.loops",))
    _record(world, a, kc, 0.9)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/mastery").text

    assert "受講者 3 名" in page
    assert "記録があるのは 1 名" in page


def test_the_trend_says_when_the_record_starts(world: World) -> None:
    """**空の区間を「まだ動いていない」と読ませない。**

    この表は移行を当てた時点から積み上がるので、それ以前は描きようがない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s1", Role.LEARNER).user_id
    kc = _kc(world, "cs.loops", "くりかえし")
    _scope(world, ("cs.loops",))
    _record(world, learner, kc, 0.4)
    _record(world, learner, kc, 0.8, at=NOW + timedelta(days=1))

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/mastery").text

    assert "記録は" in page and "からです" in page
    assert "<polyline" in page, "推移の線が出ていない"
    assert "母数が動く図です" in page


def test_the_screen_never_calls_the_estimate_measured(world: World) -> None:
    """**根拠を示せない数字はそう言う。** 予測妥当性は未測定である。"""
    world.register("teacher", Role.INSTRUCTOR)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/mastery").text

    assert "まだ測っていません" in page
    assert "学習者には出していません" in page


def _evidence(run: str) -> SkillEvidence:
    return SkillEvidence(
        grading_run_id=GradingRunId("grn_" + run * 32),
        criterion_score_id=CriterionScoreId("cs_" + "d" * 32),
        score_ratio=1.0,
        human_verified=False,
        observed_at=NOW,
    )


def test_evidence_from_another_course_is_counted_not_named(world: World) -> None:
    """**習熟度はコースを跨いで動く。**

    1 つの値に担当外の科目の観測も入っている。課題名まで出すと、担当していない
    コースの成績に近い情報を見せることになる ── 件数だけ出す。
    """
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s1", Role.LEARNER).user_id
    kc = _kc(world, "cs.loops", "くりかえし")
    _scope(world, ("cs.loops",))
    _record(world, learner, kc, 0.7, evidence=(_evidence("1"), _evidence("2")))

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/mastery/{learner}").text

    # どちらの採点も辿れない（この世界には採点結果が無い）ので、外として数える。
    assert "このコース以外から 2 件" in page
    assert "70%" in page


def test_a_learner_from_another_course_is_not_found(world: World) -> None:
    """**存在と権限を区別しない。** 他コースの受講者を列挙させない。"""
    world.register("teacher", Role.INSTRUCTOR)
    stranger = UserId("usr_" + "e" * 32)

    response = world.client("teacher").get(f"/manage/courses/{world.course.id}/mastery/{stranger}")

    assert response.status_code == 404


def test_the_enrolment_list_links_only_learners(world: World) -> None:
    """習熟度へ辿れるのは学習者だけ。教員と TA の習熟度はこの画面の問いではない。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s1", Role.LEARNER).user_id

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    assert f"/mastery/{learner}" in page
    assert page.count("/mastery/") == 1, "学習者以外にも習熟度のリンクが出ている"


def test_the_mastery_link_is_its_own_column_not_the_login(world: World) -> None:
    """**行き先が読める形で置く。**

    以前はアカウントの文字にリンクを貼っていた ── ログイン ID はメール
    アドレスのこともあり、リンクだと連絡先に見える。何より、押した先が
    何の画面なのかが読めない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s1", Role.LEARNER).user_id

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    assert "習熟度を見る" in page, "行き先を名乗るリンクになっていない"
    # ログイン ID そのものはリンクにしない。
    assert f'/mastery/{learner}">s1</a>' not in page
