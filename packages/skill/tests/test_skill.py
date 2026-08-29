"""S7 の規則を固定する。

固定したいのは 5 つ。

採点を知らない   読むのはイベントが運ぶ `KcOutcome` だけ（P6）。
冪等             同じ採点を二度受け取っても習熟度は一度しか動かない（§2.3）。
誤答で下がる     BKT の 2 段更新の順序。入れ替えると誤答直後に上がる。
確信度で止まる   人間が見ると決めた判定で学習者の記録を動かさない。
読んでいないと言う 自動確定の採点を「教員が確認した根拠」と書かない。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import GradingCompleted, KcOutcome, MasteryModel, Routing
from aijudge_core.ids import (
    CriterionScoreId,
    EventId,
    GradingRunId,
    KcId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_skill import (
    BktParameters,
    InMemorySkillRepository,
    SkillService,
    posterior,
    predict_correct,
)

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "1" * 32)
KC = KcId("kc_" + "2" * 32)
OTHER_KC = KcId("kc_" + "3" * 32)


def _event(
    *,
    run: str = "a",
    ratio: float = 1.0,
    confidence: float = 1.0,
    kcs: tuple[KcId, ...] = (KC,),
    routing: Routing = Routing.AUTO,
    provisional: bool = False,
) -> GradingCompleted:
    return GradingCompleted(
        event_id=EventId("evt_" + run * 32),
        tenant_id=TENANT,
        occurred_at=NOW,
        grading_run_id=GradingRunId("grn_" + run * 32),
        submission_id=SubmissionId("sub_" + "b" * 32),
        task_version_id=TaskVersionId("tsv_" + "c" * 32),
        learner_id=LEARNER,
        score_ratio=ratio,
        confidence=confidence,
        routing=routing,
        provisional=provisional,
        kc_outcomes=tuple(
            KcOutcome(
                kc_id=kc,
                score_ratio=ratio,
                confidence=confidence,
                criterion_score_ids=(CriterionScoreId("cs_" + "d" * 32),),
            )
            for kc in kcs
        ),
    )


def _service() -> tuple[SkillService, InMemorySkillRepository]:
    repository = InMemorySkillRepository()
    return SkillService(repository), repository


# -- BKT ------------------------------------------------------------------


def test_a_correct_answer_raises_mastery_and_a_wrong_one_lowers_it() -> None:
    params = BktParameters()
    after_right = posterior(params.prior, True, params)
    assert after_right > params.prior

    # **誤答の直後に上がってはならない。** 観測での更新より先に学習の遷移を
    # 足すとそうなる（BKT の 2 段の順序が壊れている合図）。
    assert posterior(after_right, False, params) < after_right


def test_the_prediction_is_not_the_mastery() -> None:
    """次問の正答率は習熟度そのものではない。

    Phase 4 の合格基準（AUC ≥ 0.70）が見るのはこちら。習得していても slip で
    落とし、未習得でも guess で当たるぶん、値は中央へ寄る。
    """
    params = BktParameters()
    assert predict_correct(1.0, params) == pytest.approx(1.0 - params.slip)
    assert predict_correct(0.0, params) == pytest.approx(params.guess)


def test_guessing_better_than_knowing_is_refused() -> None:
    """P(G) > 0.5 を許すと、正答が習得の証拠として働かなくなる（推定が反転）。"""
    with pytest.raises(ValueError):
        BktParameters(guess=0.6)
    with pytest.raises(ValueError):
        BktParameters(slip=0.5)


# -- サービス --------------------------------------------------------------


def test_a_completed_grading_creates_a_skill_state() -> None:
    service, repository = _service()
    updates = service.apply(_event())

    assert len(updates) == 1
    assert updates[0].previous_mastery is None
    state = repository.get_state(TENANT, LEARNER, KC)
    assert state is not None
    assert state.model is MasteryModel.BKT
    assert state.observation_count == 1
    assert state.mastery > BktParameters().prior


def test_one_grading_updates_every_kc_the_task_maps_to() -> None:
    """Q-matrix が結節点（P6）── 課題が 2 つの KC を問えば両方が動く。"""
    service, repository = _service()
    updates = service.apply(_event(kcs=(KC, OTHER_KC)))
    assert {u.kc_id for u in updates} == {KC, OTHER_KC}
    assert len(repository.list_states(TENANT, LEARNER)) == 2


def test_the_same_grading_run_is_absorbed_only_once() -> None:
    """**冪等**（§2.3）。再送で習熟度が二度上がると、配信の都合が記録に出る。"""
    service, repository = _service()
    service.apply(_event(run="a"))
    first = repository.get_state(TENANT, LEARNER, KC)

    assert service.apply(_event(run="a")) == ()
    assert repository.get_state(TENANT, LEARNER, KC) == first


def test_a_different_grading_run_does_move_the_estimate() -> None:
    service, repository = _service()
    service.apply(_event(run="a"))
    before = repository.get_state(TENANT, LEARNER, KC).mastery
    service.apply(_event(run="e"))
    assert repository.get_state(TENANT, LEARNER, KC).mastery > before


def test_a_failing_score_lowers_an_established_estimate() -> None:
    service, repository = _service()
    for run in "aef":
        service.apply(_event(run=run))
    high = repository.get_state(TENANT, LEARNER, KC).mastery

    service.apply(_event(run="9", ratio=0.1))
    assert repository.get_state(TENANT, LEARNER, KC).mastery < high


def test_a_low_confidence_judgement_does_not_move_the_record() -> None:
    """人間が見ると決めた判定で学習者の記録を動かさない。

    動かすと、教員が後で覆しても習熟度には戻らない（覆しのイベントが無い）。
    確信の無い AI 判定がポートフォリオに残り続ける。
    """
    service, repository = _service()
    assert service.apply(
        _event(confidence=0.3, routing=Routing.REVIEW_REQUIRED)
    ) == ()
    assert repository.get_state(TENANT, LEARNER, KC) is None


def test_evidence_from_an_unread_grading_is_not_marked_verified() -> None:
    """**誰も読んでいない採点を「確認済み」と書かない**（ADR 0005 / 0010）。

    書くと、ポートフォリオの「根拠 5 件（うち教員確認済み 3 件）」が嘘になる。
    """
    service, repository = _service()
    service.apply(_event())
    state = repository.get_state(TENANT, LEARNER, KC)
    assert state.evidence[0].human_verified is False
    assert state.verified_evidence_count == 0


def test_evidence_is_capped_but_the_estimate_is_not() -> None:
    """根拠の件数は上限で切るが、習熟度は全観測を畳んだ値なので減らない。"""
    repository = InMemorySkillRepository()
    service = SkillService(repository, max_evidence=3)
    for run in "abcdef":
        service.apply(_event(run=run))

    state = repository.get_state(TENANT, LEARNER, KC)
    assert len(state.evidence) == 3
    assert state.observation_count == 6


def test_a_provisional_grading_does_not_count() -> None:
    """**暫定の採点で学習者の記録を動かさない。**

    二段階キュー（ADR 0011）は 1 提出につきイベントを 2 回出す。1 回目は
    AI 観点が未採点で、総合点は決定的評価だけを比例配分したものである。
    数えると、同じ提出を二度、しかも一度目は不完全な評価で数えることになる
    ── 実際に配線して初めて出た（`observation_count` が 2 になった）。
    """
    service, repository = _service()
    assert service.apply(_event(provisional=True)) == ()
    assert repository.get_state(TENANT, LEARNER, KC) is None


def test_the_settled_grading_of_the_same_submission_does_count() -> None:
    """暫定を飛ばしても、確定した方は数える（飛ばしっぱなしにしない）。"""
    service, repository = _service()
    service.apply(_event(run="a", provisional=True))
    service.apply(_event(run="e", provisional=False))

    state = repository.get_state(TENANT, LEARNER, KC)
    assert state is not None
    assert state.observation_count == 1
