"""採点結果を観測へ投影するときに焼き込む印を固定する（Issue #685）。

測定（κ・見逃し率・レビュー行き率）は `Observation` だけを読み、採点の語彙を
知らない（ADR 0007）。だから投影が印を取り違えると、測定は黙って逆の集合で
計算する ── evalrunner や analytics のテストは `Observation` を手で組むので、
投影の誤りはそこでは捕まらない。ここで投影そのものを見る。

固定したいのは 4 つ。

auto_confirmed   AUTO の run だけが真。入れ替わるとレビュー行き率と見逃し率が
                 逆の集合で計算される。
conclusive       決定的評価の観点は（確定の印が無くても）真、AI の観点は偽。
                 決定的評価が AI の一致度の標本に入らないようにする。
unscored         理由（評価器が落ちた・打ち切った・人の採点待ち）を問わず、
                 機械の判定が無い観点は真で、段階も空。
blind            教員の段階が無ければ、blind で採点する設定でも偽。
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_human_scored_criteria import CORRECTNESS, DIAGRAM, _criterion, _task_version

from aijudge_core import (
    CharSpan,
    CriterionScore,
    EvaluatorKind,
    Evidence,
    GradingContext,
    GradingRun,
    Routing,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
)
from aijudge_grading.observations import project_observations
from aijudge_observation import Observation

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def _task() -> TaskVersion:
    """観点 2 つ。「correctness」は決定的評価、「diagram」は AI の担当。"""
    return _task_version(
        _criterion(CORRECTNESS, "correctness", 0.5, None),
        _criterion(DIAGRAM, "diagram", 0.5, None),
    )


def _deterministic(criterion_id: CriterionId, *, conclusive: bool) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId(new_id("cs")),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId(new_id("evr")),
        kind=EvaluatorKind.DETERMINISTIC,
        level=1,
        score_ratio=1.0,
        weight=0.5,
        confidence=1.0 if conclusive else 0.5,
        conclusive=conclusive,
        rationale="テストを実行した。",
    )


def _ai(criterion_id: CriterionId) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId(new_id("cs")),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId(new_id("evr")),
        kind=EvaluatorKind.AI,
        level=0,
        score_ratio=0.0,
        weight=0.5,
        confidence=0.9,
        evidence=(
            Evidence(
                artifact_id=ArtifactId(new_id("art")),
                artifact_content_hash="sha256:x",
                span=CharSpan(start=0, end=4),
            ),
        ),
        rationale="読んで判断した。",
    )


def _run(
    task: TaskVersion,
    scores: tuple[CriterionScore, ...],
    *,
    routing: Routing = Routing.AUTO,
    **reasons: tuple[CriterionId, ...],
) -> GradingRun:
    return GradingRun(
        id=GradingRunId(new_id("grn")),
        submission_id=SubmissionId(new_id("sub")),
        context=GradingContext(
            task_version_id=task.id,
            subject_profile="test_subject",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        criterion_scores=scores,
        score_ratio=0.5,
        confidence=0.9,
        routing=routing,
        created_at=NOW,
        **reasons,
    )


def _project(
    task: TaskVersion,
    run: GradingRun,
    *,
    human_levels: dict[str, int] | None = None,
    blind: bool = False,
) -> dict[str, Observation]:
    rows = project_observations(
        run,
        task,
        subject_profile="test_subject",
        task_name="ex01",
        submission="s0001",
        observed_at=NOW,
        human_levels=human_levels,
        blind=blind,
        marker="teacher",
    )
    return {row.criterion_code: row for row in rows}


# -- auto_confirmed ----------------------------------------------------------


def test_an_auto_routed_run_is_projected_as_auto_confirmed() -> None:
    task = _task()
    rows = _project(task, _run(task, (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM))))
    assert all(row.auto_confirmed for row in rows.values())


def test_a_run_sent_to_review_is_not_projected_as_auto_confirmed() -> None:
    """**入れ替わると見逃し率とレビュー行き率が逆の集合で計算される。**"""
    task = _task()
    run = _run(
        task,
        (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM)),
        routing=Routing.REVIEW_REQUIRED,
    )
    rows = _project(task, run)
    assert not any(row.auto_confirmed for row in rows.values())


# -- conclusive --------------------------------------------------------------


def test_a_conclusive_deterministic_criterion_is_conclusive() -> None:
    task = _task()
    rows = _project(task, _run(task, (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM))))
    assert rows["correctness"].conclusive is True


def test_a_deterministic_criterion_counts_as_conclusive_even_unmarked() -> None:
    """確定の印が無くても、決定的評価の判定は AI の精度の標本に入れない。

    text_pattern_check は線引きの決まらない観点を `conclusive=False` で返す。
    これを AI の一致度に混ぜると、AI が判定していない観点で AI の精度を測る。
    """
    task = _task()
    rows = _project(task, _run(task, (_deterministic(CORRECTNESS, conclusive=False), _ai(DIAGRAM))))
    assert rows["correctness"].conclusive is True


def test_an_ai_criterion_is_not_conclusive() -> None:
    task = _task()
    rows = _project(task, _run(task, (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM))))
    assert rows["diagram"].conclusive is False


# -- unscored ----------------------------------------------------------------


def test_a_scored_criterion_is_not_unscored() -> None:
    task = _task()
    rows = _project(task, _run(task, (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM))))
    assert rows["diagram"].unscored is False
    assert rows["diagram"].machine_level == 0


def _assert_unscored(row: Observation) -> None:
    assert row.unscored is True
    assert row.machine_level is None
    assert row.machine_confidence is None


def test_a_criterion_whose_evaluator_failed_is_unscored() -> None:
    task = _task()
    run = _run(
        task,
        (_deterministic(CORRECTNESS, conclusive=True),),
        routing=Routing.REVIEW_REQUIRED,
        unscored_criteria=(DIAGRAM,),
    )
    _assert_unscored(_project(task, run)["diagram"])


def test_a_criterion_cut_by_the_gate_is_unscored() -> None:
    """**打ち切りも標本に入れない。** 確定した 0% だが、比べる機械の判定が無い。

    `unscored_criteria` だけを見る形に狭めると、ここが偽になる。
    """
    task = _task()
    run = _run(task, (_deterministic(CORRECTNESS, conclusive=True),), skipped_criteria=(DIAGRAM,))
    _assert_unscored(_project(task, run)["diagram"])


def test_a_criterion_awaiting_a_human_is_unscored() -> None:
    task = _task()
    run = _run(
        task,
        (_deterministic(CORRECTNESS, conclusive=True),),
        routing=Routing.REVIEW_REQUIRED,
        awaiting_human=(DIAGRAM,),
    )
    _assert_unscored(_project(task, run)["diagram"])


# -- blind -------------------------------------------------------------------


def test_blind_without_human_levels_is_not_blind() -> None:
    """教員の段階が無い提出は blind 採点の記録ではない（抽出対象外）。"""
    task = _task()
    rows = _project(
        task,
        _run(task, (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM))),
        human_levels=None,
        blind=True,
    )
    assert not any(row.blind for row in rows.values())
    assert all(row.marker is None for row in rows.values())


def test_blind_with_human_levels_is_blind() -> None:
    task = _task()
    rows = _project(
        task,
        _run(task, (_deterministic(CORRECTNESS, conclusive=True), _ai(DIAGRAM))),
        human_levels={"diagram": 1},
        blind=True,
    )
    assert rows["diagram"].blind is True
    assert rows["diagram"].human_level == 1
    assert rows["diagram"].usable_for_agreement is True
