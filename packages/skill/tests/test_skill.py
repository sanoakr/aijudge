"""S7 の規則を固定する。

固定したいのは 5 つ。

採点を知らない   読むのはイベントが運ぶ `KcOutcome` だけ（P6）。
冪等             同じ採点を二度受け取っても習熟度は一度しか動かない（§2.3）。
誤答で下がる     BKT の 2 段更新の順序。入れ替えると誤答直後に上がる。
確信度で止まる   人間が見ると決めた判定で学習者の記録を動かさない。
読んでいないと言う 自動確定の採点を「教員が確認した根拠」と書かない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import (
    GradingCompleted,
    KcOutcome,
    MasteryModel,
    MasteryScope,
    Routing,
    SkillState,
)
from aijudge_core.ids import (
    CriterionScoreId,
    EventId,
    GradingRunId,
    KcId,
    SubmissionId,
    TaskId,
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
    task: str | None = None,
    exam: bool = False,
    submitted_at: datetime | None = None,
) -> GradingCompleted:
    """採点完了のイベント。**既定では採点ごとに別の課題**（`task` を省くと `run` から作る）。

    同じ課題を出し直した場合は `task` を揃えて書く（ADR 0032 の置き換え）。
    """
    return GradingCompleted(
        event_id=EventId("evt_" + run * 32),
        tenant_id=TENANT,
        occurred_at=NOW,
        grading_run_id=GradingRunId("grn_" + run * 32),
        submission_id=SubmissionId("sub_" + run * 32),
        task_version_id=TaskVersionId("tsv_" + "c" * 32),
        learner_id=LEARNER,
        score_ratio=ratio,
        confidence=confidence,
        routing=routing,
        provisional=provisional,
        task_id=TaskId("tsk_" + (task or run) * 32),
        exam=exam,
        submitted_at=submitted_at or NOW,
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
    assert service.apply(_event(confidence=0.3, routing=Routing.REVIEW_REQUIRED)) == ()
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


# -- 推移の記録（#328）-----------------------------------------------------


def test_every_move_leaves_a_point() -> None:
    """**動いた瞬間を残す。** `SkillState` は最新の 1 行を持ち替えるので、
    ここで残さないと推移は二度と読めない（根拠は 20 件で切られ、BKT は観測列を
    畳むので遡れない）。
    """
    service, repository = _service()
    service.apply(_event(run="a"))
    service.apply(_event(run="b", ratio=0.0))

    points = repository.history(TENANT, (LEARNER,))

    assert [p.observation_count for p in points] == [1, 2], "観測数が積み上がっていない"
    assert points[0].mastery > points[1].mastery, "誤答のあとが下がっていない"
    assert all(p.kc_id == KC for p in points)
    assert all(p.model is MasteryModel.BKT for p in points)


def test_a_move_that_did_not_happen_leaves_no_point() -> None:
    """**呼ぶのは実際に動いたときだけ。**

    再送（冪等）と確信度不足では習熟度が動かない。そこで点を残すと、推移の
    図に「同じ採点を二度受け取った」という配信の都合が出てしまう。
    """
    service, repository = _service()
    service.apply(_event(run="a"))
    service.apply(_event(run="a"))  # 同じ採点の再送
    service.apply(_event(run="c", confidence=0.1))  # 確信度が足りない

    assert len(repository.history(TENANT, (LEARNER,))) == 1


def test_history_needs_a_learner() -> None:
    """**空は「全員」ではない。** 取り違えると、テナント全員の推移が
    1 コースの画面に並ぶ。
    """
    service, repository = _service()
    service.apply(_event(run="a"))

    assert repository.history(TENANT, ()) == ()
    assert repository.list_states_for(TENANT, ()) == ()


def test_history_can_be_narrowed_to_one_kc() -> None:
    service, repository = _service()
    service.apply(_event(run="a", kcs=(KC, OTHER_KC)))

    only = repository.history(TENANT, (LEARNER,), kc_ids=(OTHER_KC,))

    assert [p.kc_id for p in only] == [OTHER_KC]


def test_states_can_be_read_for_several_learners_at_once() -> None:
    """人数ぶん `list_states` を呼ばせない（コースの画面が N+1 になる）。"""
    service, repository = _service()
    service.apply(_event(run="a"))

    assert len(repository.list_states_for(TENANT, (LEARNER,))) == 1
    assert repository.list_states_for(TENANT, (UserId("usr_" + "9" * 32),)) == ()


# -- 課題ごとの最終提出と、試験の重み（ADR 0032）---------------------------


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def test_resubmitting_a_task_replaces_its_observation() -> None:
    """**出し直しは足さずに置き換える。** 誤答を 4 回出してから通っても、観測は 1 件。"""
    service, repository = _service()
    for i, run in enumerate("abcd"):
        service.apply(_event(run=run, task="t", ratio=0.0, submitted_at=_at(i)))
    service.apply(_event(run="e", task="t", ratio=1.0, submitted_at=_at(9)))

    state = repository.get_state(TENANT, LEARNER, KC)
    assert state.observation_count == 1
    only_the_final = _service()
    only_the_final[0].apply(_event(run="e", task="t", ratio=1.0, submitted_at=_at(9)))
    assert state.mastery == pytest.approx(only_the_final[1].get_state(TENANT, LEARNER, KC).mastery)


def test_a_regrade_replaces_the_observation_of_the_same_submission() -> None:
    """再採点も足さない。同じ提出の新しい採点で置き換える。"""
    service, repository = _service()
    service.apply(_event(run="a", task="t", ratio=1.0))
    service.apply(_event(run="e", task="t", ratio=0.0))  # 同じ提出時刻・別の採点

    state = repository.get_state(TENANT, LEARNER, KC)
    assert state.observation_count == 1
    assert state.evidence[-1].grading_run_id == GradingRunId("grn_" + "e" * 32)


def test_an_older_submission_does_not_overwrite_a_newer_one() -> None:
    """**採点の終わった順ではなく、提出の順。** 遅れて届いた古い提出は無視する。"""
    service, repository = _service()
    service.apply(_event(run="e", task="t", ratio=1.0, submitted_at=_at(10)))
    before = repository.get_state(TENANT, LEARNER, KC)

    assert service.apply(_event(run="a", task="t", ratio=0.0, submitted_at=_at(5))) == ()
    assert repository.get_state(TENANT, LEARNER, KC) == before


def test_resubmitting_the_same_result_leaves_no_point() -> None:
    """正答を正答で出し直しても、推移に点を残さない（出した回数を図に出さない）。"""
    service, repository = _service()
    service.apply(_event(run="a", task="t", submitted_at=_at(0)))
    service.apply(_event(run="e", task="t", submitted_at=_at(1)))

    assert len(repository.history(TENANT, (LEARNER,))) == 1


def test_an_exam_answer_weighs_more_than_a_practice_answer() -> None:
    """**監督のある試験の正答は重く、演習の正答は軽く**（当て推量が違う）。"""
    practice, practice_repo = _service()
    practice.apply(_event(run="a"))
    exam, exam_repo = _service()
    exam.apply(_event(run="a", exam=True))

    assert practice_repo.get_state(TENANT, LEARNER, KC).mastery == pytest.approx(0.456)
    assert exam_repo.get_state(TENANT, LEARNER, KC).mastery == pytest.approx(0.696)


def test_an_exam_failure_pulls_down_what_practice_built_up() -> None:
    """演習で高く出ても、試験の誤答で引き戻される。"""

    def after(*, exam: bool) -> SkillState:
        service, repository = _service()
        for i, run in enumerate("abc"):
            service.apply(_event(run=run, submitted_at=_at(i)))
        service.apply(_event(run="e", exam=exam, ratio=0.0, submitted_at=_at(9)))
        return repository.get_state(TENANT, LEARNER, KC)

    after_exam = after(exam=True)
    assert after_exam.mastery < after(exam=False).mastery
    assert after_exam.evidence[-1].exam is True


def test_observations_are_folded_in_submission_order() -> None:
    """畳む順は提出の順。採点の届いた順が入れ替わっても結果は同じ。"""
    first, first_repo = _service()
    first.apply(_event(run="a", ratio=0.0, submitted_at=_at(0)))
    first.apply(_event(run="e", ratio=1.0, exam=True, submitted_at=_at(5)))
    swapped, swapped_repo = _service()
    swapped.apply(_event(run="e", ratio=1.0, exam=True, submitted_at=_at(5)))
    swapped.apply(_event(run="a", ratio=0.0, submitted_at=_at(0)))

    assert first_repo.get_state(TENANT, LEARNER, KC).mastery == pytest.approx(
        swapped_repo.get_state(TENANT, LEARNER, KC).mastery
    )


def test_an_event_without_a_task_falls_back_to_the_task_version() -> None:
    """この欄より前に積まれたイベントも数える（課題の代わりに版で分ける）。"""
    service, repository = _service()
    event = _event(run="a").model_copy(update={"task_id": None, "submitted_at": None})
    service.apply(event)

    observations = repository.list_observations(TENANT, LEARNER, KC)
    assert [str(item.task_id) for item in observations] == [str(event.task_version_id)]


# -- 演習だけ・試験だけ・両方（ADR 0032）------------------------------------


def test_practice_and_exam_are_also_folded_on_their_own() -> None:
    """3 通りを持つ。**観測の無い範囲は None**（0 ではない）。"""
    service, repository = _service()
    service.apply(_event(run="a", submitted_at=_at(0)))
    state = repository.get_state(TENANT, LEARNER, KC)
    assert state.mastery_in(MasteryScope.PRACTICE) == pytest.approx(0.456)
    assert state.mastery_in(MasteryScope.EXAM) is None
    assert state.count_in(MasteryScope.EXAM) == 0

    service.apply(_event(run="e", exam=True, ratio=0.0, submitted_at=_at(5)))
    state = repository.get_state(TENANT, LEARNER, KC)
    # 演習だけの値は試験の誤答で動かない。試験だけの値は試験の誤答 1 件ぶん。
    assert state.mastery_in(MasteryScope.PRACTICE) == pytest.approx(0.456)
    assert state.mastery_in(MasteryScope.EXAM) == pytest.approx(0.088)
    assert state.count_in(MasteryScope.ALL) == 2
    assert state.mastery_in(MasteryScope.ALL) < state.mastery_in(MasteryScope.PRACTICE)


def test_each_scope_keeps_its_own_trend() -> None:
    """推移も範囲ごとに残す。**動いた範囲だけ**点を足す。"""
    service, repository = _service()
    service.apply(_event(run="a", submitted_at=_at(0)))
    service.apply(_event(run="e", exam=True, submitted_at=_at(5)))

    def counts(scope: MasteryScope) -> list[int]:
        return [p.observation_count for p in repository.history(TENANT, (LEARNER,), scope=scope)]

    assert counts(MasteryScope.ALL) == [1, 2]
    assert counts(MasteryScope.PRACTICE) == [1]  # 試験の観測では動かない
    assert counts(MasteryScope.EXAM) == [1]
