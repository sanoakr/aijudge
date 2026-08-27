"""採点結果。

ここで守る設計原則は 3 つ。

P3  決定的評価が先、AI は後。決定的評価が「不正解」を確定させた観点は
    AI で覆さない。この規則は集約関数 `aggregate` に実装として書く。
P4  すべての CriterionScore は根拠（Evidence）を持つ。
P8  GradingRun は不変。人間の修正も上書きではなく HumanReview として追記する。
    AI と人間の差分がそのまま採点一致率（κ）の測定データになる。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    HumanReviewId,
    KcId,
    SubmissionId,
    TaskVersionId,
    UserId,
)
from .spans import Evidence


class EvaluatorKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AI = "ai"


class EvaluatorStatus(StrEnum):
    """評価器の実行結果。失敗は例外にせず結果として記録する（§04 step 2）。"""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class Routing(StrEnum):
    AUTO = "auto"
    REVIEW_REQUIRED = "review_required"


class GradingContext(BaseModel):
    """この採点を再現するために必要な全て（P8）。

    同一入力の再採点、モデル更新時の全件再評価、年度をまたいだ公平性の検証は
    すべてこの値が揃っていて初めて可能になる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_version_id: TaskVersionId
    subject_profile: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    # 入力 Artifact の content_hash を並べたもの。再採点の同一性判定に使う。
    input_hash: str = Field(min_length=1)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    model_ids: dict[str, str] = Field(default_factory=dict)
    model_params: dict[str, object] = Field(default_factory=dict)
    pipeline_version: str = Field(min_length=1)


class EvaluatorResult(BaseModel):
    """評価器 1 個分の生の出力。デバッグと再現のために捨てずに残す。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: EvaluatorResultId
    evaluator_id: str = Field(min_length=1)
    kind: EvaluatorKind
    status: EvaluatorStatus = EvaluatorStatus.OK
    duration_ms: int = Field(default=0, ge=0)
    raw_output: dict[str, object] = Field(default_factory=dict)
    error: str | None = None

    @model_validator(mode="after")
    def _check_error(self) -> Self:
        if self.status is EvaluatorStatus.FAILED and not self.error:
            raise ValueError("a failed EvaluatorResult must carry an error message")
        return self


class CriterionScore(BaseModel):
    """ルーブリック観点 1 つの採点結果。

    `conclusive` は「決定的評価がこの観点を確定させた」ことを表す。
    True の観点は AI 評価器の判定で上書きされない（P3）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CriterionScoreId
    criterion_id: CriterionId
    evaluator_result_id: EvaluatorResultId
    kind: EvaluatorKind
    level: int = Field(ge=0)
    score_ratio: float = Field(ge=0.0, le=1.0)
    weight: float = Field(gt=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    conclusive: bool = False
    evidence: tuple[Evidence, ...] = ()
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_conclusive(self) -> Self:
        if self.conclusive and self.kind is not EvaluatorKind.DETERMINISTIC:
            raise ValueError("only a deterministic evaluator may produce a conclusive score")
        return self

    @property
    def weighted(self) -> float:
        return self.score_ratio * self.weight


class KcOutcome(BaseModel):
    """KC 単位に畳んだ結果。S7（スキル推定）が購読するのはこれ（P6）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kc_id: KcId
    score_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    criterion_score_ids: tuple[CriterionScoreId, ...] = Field(min_length=1)


class ReviewPolicy(BaseModel):
    """人間レビューへ回す条件（§04 step 5）。科目 YAML から与える。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confidence_below: float = Field(default=0.75, ge=0.0, le=1.0)
    always_review_if_weight_over: float = Field(default=1.0, gt=0.0, le=1.0)
    # 合否境界の上下このマージン内なら人間が見る。
    boundary_score: float | None = None
    boundary_margin: float = Field(default=0.0, ge=0.0)

    def requires_review(self, scores: tuple[CriterionScore, ...], total_ratio: float) -> bool:
        for score in scores:
            if score.conclusive:
                continue
            if score.confidence < self.confidence_below:
                return True
            if score.weight > self.always_review_if_weight_over:
                return True
        # 合否境界の近傍は、確信度が高くても人間が見る。
        return (
            self.boundary_score is not None
            and abs(total_ratio - self.boundary_score) <= self.boundary_margin
        )


class BlindMark(BaseModel):
    """教員が **AI の判定を見る前に** 付けた段階。

    測定の正解データはこれだけ。AI を見たあとの段階（`HumanReview`）は
    AI に引きずられており（アンカリング）、一致度の標本にならない（ADR 0005）。

    全提出に求めるものではない。抽出された提出だけに求める（ADR 0007）。
    採点そのものはこれを待たない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    submission_id: SubmissionId
    grader_id: UserId
    # 観点コードではなく ID で持つ。コードは課題版ごとに付け替えられる。
    levels: dict[CriterionId, int] = Field(min_length=1)
    marked_at: datetime
    notes: str | None = None


class HumanReview(BaseModel):
    """教員による確認・修正。GradingRun を上書きせず追記する。

    **この記録が成績の確定を意味する。** 存在しないうちは AI の判定は
    暫定であり、学習者に示す範囲は上位層が決める（設計原則 P5）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: HumanReviewId
    grading_run_id: GradingRunId
    grader_id: UserId
    # 変更した観点だけを持つ。触っていない観点は AI の判定に同意したという意味。
    adjusted_levels: dict[CriterionId, int] = Field(default_factory=dict)
    comment: str | None = None
    reviewed_at: datetime

    @property
    def agreed(self) -> bool:
        """AI の採点をそのまま承認したか。κ の算出に使う。"""
        return not self.adjusted_levels

    def level_for(self, criterion_id: CriterionId, machine_level: int) -> int:
        """確定した段階。教員が触っていなければ機械の判定がそのまま通る。"""
        return self.adjusted_levels.get(criterion_id, machine_level)


class GradingRun(BaseModel):
    """1 回の採点。作成後は不変（P8）。

    再採点は新しい GradingRun を作る。過去の run は決して書き換えない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: GradingRunId
    submission_id: SubmissionId
    context: GradingContext
    evaluator_results: tuple[EvaluatorResult, ...] = ()
    criterion_scores: tuple[CriterionScore, ...] = Field(min_length=1)
    kc_outcomes: tuple[KcOutcome, ...] = ()
    score_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    routing: Routing
    feedback: str | None = None
    # 評価器の失敗などで採点できなかった観点。空でなければ点は暫定であり、
    # routing は必ず REVIEW_REQUIRED になる。
    unscored_criteria: tuple[CriterionId, ...] = ()
    created_at: datetime
    superseded_by: GradingRunId | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        criterion_ids = [score.criterion_id for score in self.criterion_scores]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("each criterion may be scored at most once per GradingRun")
        known = {result.id for result in self.evaluator_results}
        for score in self.criterion_scores:
            if self.evaluator_results and score.evaluator_result_id not in known:
                raise ValueError(
                    f"CriterionScore {score.id!r} references an unknown EvaluatorResult"
                )
            if score.kind is EvaluatorKind.AI and not score.evidence:
                # P4: AI はスコアだけを返してはならない。
                raise ValueError(f"AI CriterionScore {score.id!r} must carry evidence")
        return self

    @model_validator(mode="after")
    def _check_unscored(self) -> Self:
        if self.unscored_criteria and self.routing is not Routing.REVIEW_REQUIRED:
            # 誰も見ていない観点がある採点を自動確定させない（設計原則 P5）。
            raise ValueError("a run with unscored criteria must be routed to review")
        return self

    @property
    def needs_review(self) -> bool:
        return self.routing is Routing.REVIEW_REQUIRED

    @property
    def is_provisional(self) -> bool:
        return bool(self.unscored_criteria)


def aggregate(scores: tuple[CriterionScore, ...]) -> tuple[float, float]:
    """観点別スコアを総合点と確信度に畳む。

    決定的評価が確定させた観点（conclusive）は重みどおりに効き、
    その観点について AI の判定は最初から入ってこない（呼び出し側が排除する）。
    総合の確信度は、確定していない観点の確信度の最小値とする。
    平均ではなく最小を採るのは、ひとつでも自信のない観点があれば
    その採点全体を人間が見るべきだから。
    """
    if not scores:
        raise ValueError("cannot aggregate an empty score set")

    total_weight = sum(score.weight for score in scores)
    if abs(total_weight - 1.0) > 1e-6:
        raise ValueError(f"criterion weights must sum to 1.0, got {total_weight}")

    score_ratio = sum(score.weighted for score in scores)
    uncertain = [score.confidence for score in scores if not score.conclusive]
    confidence = min(uncertain) if uncertain else 1.0
    return round(score_ratio, 10), confidence


def renormalize(scores: tuple[CriterionScore, ...]) -> tuple[CriterionScore, ...]:
    """一部の観点が採点されなかったとき、残った観点の重みを比例配分し直す。

    評価器が落ちた観点を 0 点にすると学習者に不当な不利益が出るし、
    満点にすると誰も見ていない観点に点を与えることになる。どちらも取らず、
    採点できた観点だけで暫定の点を出し、**必ず人間のレビューに回す**
    （呼び出し側の責務）。GradingRun には未採点の観点を記録する。
    """
    if not scores:
        raise ValueError("cannot renormalize an empty score set")
    total = sum(score.weight for score in scores)
    if total <= 0.0:
        raise ValueError("total weight must be positive")
    return tuple(score.model_copy(update={"weight": score.weight / total}) for score in scores)


def resolve_conflicts(scores: tuple[CriterionScore, ...]) -> tuple[CriterionScore, ...]:
    """同一観点に複数の評価器が答えた場合の優先順位を決める（P3）。

    決定的評価があればそれを採用し、AI の判定は捨てる。
    決定的評価どうしが競合することは想定しない（設定ミスなので例外にする）。
    """
    by_criterion: dict[CriterionId, list[CriterionScore]] = {}
    for score in scores:
        by_criterion.setdefault(score.criterion_id, []).append(score)

    resolved: list[CriterionScore] = []
    for criterion_id, candidates in by_criterion.items():
        deterministic = [s for s in candidates if s.kind is EvaluatorKind.DETERMINISTIC]
        if len(deterministic) > 1:
            raise ValueError(
                f"criterion {criterion_id!r} has multiple deterministic scores; "
                "check the subject profile"
            )
        resolved.append(deterministic[0] if deterministic else candidates[0])
    return tuple(resolved)
