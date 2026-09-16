"""学習者が自分の習熟度を見る画面（#335）。

固定したいのは 4 つ。

同じ部品     教員が受講者 1 人を見る画面と同じものを描く。
推定と言う   成績ではなく、妥当性が未測定であることを画面が言う。
確信度       数値と図の両方。**記録が無いものを 0% と書かない。**
根拠は畳む   提出の一覧は開いたときだけ出る（概観が目当ての人を妨げない）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_studentweb import COURSE, World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import MasteryModel, SkillEvidence, SkillState
from aijudge_core.ids import CriterionScoreId, GradingRunId, KcId
from aijudge_core.knowledge import KnowledgeComponent

LOOPS = KcId("kc_" + "a" * 32)
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def _record(world: World, *, mastery: float = 0.77, confidence: float | None = 0.82) -> None:
    """語彙・コースの範囲・習熟度を用意する。"""
    with world.database.unit_of_work() as uow:
        uow.skills.save_kc(
            KnowledgeComponent(
                id=LOOPS,
                namespace="cs",
                path=("sdf", "fundamentals", "definite_loop"),
                label="回数の決まったくりかえし",
            )
        )
        course = uow.identity.get_course(COURSE)
        uow.identity.save_course(
            course.model_copy(
                update={"knowledge_components": ("cs.sdf.fundamentals.definite_loop",)}
            )
        )
        learner = uow.identity.find_user_by_login(course.tenant_id, "s2400001")
        uow.skills.save_state(
            SkillState(
                tenant_id=course.tenant_id,
                learner_id=learner.id,
                kc_id=LOOPS,
                mastery=mastery,
                model=MasteryModel.BKT,
                observation_count=2,
                evidence=(
                    SkillEvidence(
                        grading_run_id=GradingRunId("grn_" + "1" * 32),
                        criterion_score_id=CriterionScoreId("cs_" + "d" * 32),
                        score_ratio=0.9,
                        confidence=confidence,
                        human_verified=False,
                        observed_at=NOW,
                    ),
                ),
                updated_at=NOW,
            )
        )
        uow.commit()


def _page(world: World) -> str:
    world.register("s2400001")
    world.login("s2400001")
    _record(world)
    return world.client.get(f"/courses/{COURSE}/mastery").text


def test_the_learner_sees_the_numbers_and_the_bars(world: World) -> None:
    """**数値と図の両方。** 図だけだと 62% と 64% が読み分けられず、
    数値だけだと知識要素どうしを見比べるのに読み上げが要る。
    """
    page = _page(world)

    assert "回数の決まったくりかえし" in page
    assert "77%" in page, "習熟度の数値が出ていない"
    assert "82%" in page, "確信度の数値が出ていない"
    assert 'class="kcbar-fill mastery"' in page
    assert 'class="kcbar-fill confidence"' in page


def test_it_says_this_is_an_estimate_not_a_grade(world: World) -> None:
    """**黙って数字を出さない。** 学習者は確定した評価として読む。"""
    page = _page(world)

    assert "推定値です" in page
    assert "成績ではありません" in page
    assert "まだ測っていません" in page


def test_the_evidence_is_folded_away(world: World) -> None:
    """概観が目当ての人に、提出の一覧を先に読ませない。"""
    page = _page(world)

    assert "<details" in page
    assert "根拠となった提出" in page


def test_a_missing_confidence_is_not_written_as_zero(world: World) -> None:
    """**記録が無いことを 0% と書かない**（#335）。

    この欄より前に積まれた根拠は確信度を持たない ── 0 として数えると、
    古い記録ほど自信が無かったことになる。
    """
    world.register("s2400001")
    world.login("s2400001")
    _record(world, confidence=None)

    page = world.client.get(f"/courses/{COURSE}/mastery").text

    assert "記録なし" in page
    assert 'class="kcbar-fill confidence"' not in page


def test_nothing_recorded_says_so(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")

    page = world.client.get(f"/courses/{COURSE}/mastery").text

    assert "まだ記録がありません" in page
