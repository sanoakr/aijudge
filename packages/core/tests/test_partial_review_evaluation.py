"""欠けを埋めないレビューが当たったときの評価点と保留を固定する（#684）。

評価器が落ちて重みを比例配分し直した run（C1 の重み 0.6 → 1.0、C2 は unscored）に、
C1 だけを直した `HumanReview` が付く場合を考える。段階が全観点そろっていないので、
`_reviewed_evaluation` は機械が採点した観点の重み（比例配分済みの 1.0）で暫定を
出す。ルーブリックの重み（0.6）で数えると、未採点の C2 を 0 点として数えた値になる。
`if complete:` を `if True:` にしてもスイートは通っていた。

コンソールの確定・訂正は全観点の入力を求める（`_parse_levels`）ので、画面からは
この形のレビューを作れない。それでも別経路で保存されれば値はそのまま成績に入るので、
評価点の期待値と、そのとき総合点を保留するかを決めておく。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    HumanReview,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    final_score,
    score_withheld,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    HumanReviewId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
VERSION = TaskVersionId("tsv_" + "2" * 32)
C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)


def _task() -> TaskVersion:
    levels = (
        RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
        RubricLevel(level=1, label="一部", descriptor="半分", score_ratio=0.5),
        RubricLevel(level=2, label="達成", descriptor="十分", score_ratio=1.0),
    )
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="## 課題 ##\n\n出しなさい。",
        criteria=tuple(
            RubricCriterion(
                id=criterion_id,
                code=code,
                title=code,
                description=f"{code} を見る観点。",
                weight=weight,
                levels=levels,
            )
            for criterion_id, code, weight in ((C1, "correctness", 0.6), (C2, "style", 0.4))
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )


def _broken_run() -> GradingRun:
    """C2 の評価器が落ち、C1 の重みを 0.6 → 1.0 に比例配分し直した run。"""
    return GradingRun(
        id=GradingRunId("grn_" + "a" * 32),
        submission_id=SubmissionId("sub_" + "b" * 32),
        context=GradingContext(
            task_version_id=VERSION,
            subject_profile="cs_lang_c_intro",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "1" * 32),
                criterion_id=C1,
                evaluator_result_id=EvaluatorResultId("evr_" + "d" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=2,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="テストを実行した。",
            ),
        ),
        unscored_criteria=(C2,),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.REVIEW_REQUIRED,
        created_at=NOW,
    )


def _review(levels: dict[CriterionId, int]) -> HumanReview:
    return HumanReview(
        id=HumanReviewId("hrv_" + "e" * 32),
        grading_run_id=GradingRunId("grn_" + "a" * 32),
        grader_id=UserId("usr_" + "f" * 32),
        adjusted_levels=levels,
        comment="出力を確認して段階を直しました。根拠は以上です。",
        reviewed_at=NOW,
    )


def test_a_review_that_leaves_the_gap_uses_the_renormalised_weights() -> None:
    """そろっていない段階は、比例配分済みの重みで暫定を出す。

    C1 を「一部」（0.5）に下げただけなら 0.5 × 1.0 = 0.5。ルーブリックの重みで
    数えると 0.5 × 0.6 = 0.3 になり、未採点の C2 を 0 点として数えたことになる。
    """
    evaluation = final_score(_broken_run(), _task(), _review({C1: 1})).evaluation
    assert evaluation == pytest.approx(0.5)


def test_a_review_that_fills_the_gap_uses_the_rubric_weights() -> None:
    """欠けが埋まれば、ルーブリックの本来の重みで数える。保留も解ける。"""
    run = _broken_run()
    review = _review({C1: 1, C2: 2})
    assert final_score(run, _task(), review).evaluation == pytest.approx(0.5 * 0.6 + 1.0 * 0.4)
    assert not score_withheld(run, review)


def test_a_review_that_leaves_the_gap_keeps_the_total_withheld() -> None:
    """欠けを埋めないレビューでは、総合点を保留したままにする。

    `score_withheld` の説明は「教員が欠けを埋めれば保留は解ける」、
    `_reviewed_evaluation` の説明は「そろっていなければ…その総合点は保留されて
    いる」で、どちらも埋まるまで保留する側を述べている。実装はレビューの有無
    だけを見るので、C1 だけを直したレビューでも 0.5 が保留なしで出る。
    """
    assert score_withheld(_broken_run(), _review({C1: 1}))
