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
    ReviewRequestId,
    SubmissionId,
    TaskVersionId,
    UserId,
)
from .spans import Evidence
from .task import Aggregation, RubricCriterion, TaskVersion


class EvaluatorKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AI = "ai"


class EvaluatorStatus(StrEnum):
    """評価器の実行結果。失敗は例外にせず結果として記録する（§04 step 2）。"""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class GradingPhase(StrEnum):
    """採点の段階。**キューを分けるためにある。**

    決定的評価は 1 秒未満、AI 評価は十数秒かかる（実測 12.8 秒、うち 95% が
    LLM）。同じキューに並べると、テスト実行が終わっている提出の結果が、
    前に並んだ他人の LLM 待ちの後ろで止まる。締切前のバーストでは、それが
    そのまま学習者の待ち時間になる ── 実測 520 件 2 時間で、1 ワーカーの
    平均が 74 秒、p95 が 176 秒（設計方針 §9.1 の「p95 < 30 秒」を大きく外す）。

    分ければ、決定的評価の結果は先に返り、**AI 評価はあとから届く**
    （設計方針 §9.1・§10 が最初からそう書いている形）。
    """

    DETERMINISTIC = "deterministic"
    """テスト実行・静的解析など。速い。学習者に先に返す分。"""

    AI = "ai"
    """ルーブリック観点の LLM 判定。遅い。あとから届く分。"""


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


# 根拠説明の最短長。1〜2 文字の「ok」「違う」を根拠として通さない。
MIN_JUSTIFICATION_LENGTH = 10


class ReviewRequest(BaseModel):
    """学習者からの再確認の依頼。

    AI の判定は採点直後に学習者へ示す。誤りを疑ったときの導線がなければ、
    示したこと自体が一方的な通告になる（設計方針 §9.4 の「異議申し立て
    導線」）。

    **根拠説明を必須にする。** 「納得できない」だけの依頼を受け付けると、
    教員は何を確認すべきか分からないまま全件を見ることになり、導線が
    機能しなくなる。学習者にとっても、どこが違うと考えるかを書く過程が
    自己評価になる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ReviewRequestId
    submission_id: SubmissionId
    grading_run_id: GradingRunId
    learner_id: UserId
    # 学習者が「どの観点のどこが違うと考えるか」。必須。
    reason: str = Field(min_length=MIN_JUSTIFICATION_LENGTH)
    # 対象の観点。空なら全体に対する依頼。
    criterion_ids: tuple[CriterionId, ...] = ()
    requested_at: datetime
    # 対応した教員レビュー。未対応なら None。
    resolved_by: HumanReviewId | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_by is not None


class LatePenaltyStep(BaseModel):
    """遅延の段。「この時間を超えたらこの割合を引く」。

    `after_hours` は締切からの超過時間で、`ratio` は総合点比から差し引く量。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    after_hours: float = Field(ge=0.0)
    ratio: float = Field(ge=0.0, le=1.0)


class LatePenalty(BaseModel):
    """提出が遅れたことによる減点。**評価ではない。**

    採点は遅延を知らない。評価器は本文と提出物だけを見て観点の段階を決め、
    遅延はその結果に対する減点として外から当てる。混ぜると 2 つ壊れる。

    - 観点の一致度（κ）が「読解」と「事務」の混合を測ることになる。実際に
      2023 年度の採点表では体裁の段階に `遅延14h` が畳み込まれていた。
    - 習熟度（S7）に遅延が混ざる。何ができるかと、いつ出したかは別の事実である。

    **記録する。表示のたびに計算し直さない**（P8）。規則は `Course` が持ち、
    学期の途中で教員が変えうる。計算し直す作りにすると、規則を変えた瞬間に
    過去の成績が黙って動く。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 総合点比から差し引く量。
    ratio: float = Field(ge=0.0, le=1.0)
    hours_late: float = Field(gt=0.0)
    due_at: datetime
    submitted_at: datetime
    # 学習者に示す根拠（P4）。どの段が当たったかを言葉で持つ。
    reason: str = Field(min_length=1)


def late_penalty_for(
    due_at: datetime | None,
    submitted_at: datetime | None,
    steps: tuple[LatePenaltyStep, ...],
) -> LatePenalty | None:
    """締切と提出時刻から減点を決める。当たらなければ None。

    **締切も規則も無いときは減点しない。** 「見ていない」ことを黙って
    満点に化けさせないために、見たかどうかは呼び出し側が理由に書く。
    """
    if due_at is None or submitted_at is None or not steps:
        return None
    if submitted_at <= due_at:
        return None

    hours = (submitted_at - due_at).total_seconds() / 3600
    applicable = [step for step in steps if hours > step.after_hours]
    if not applicable:
        return None
    step = max(applicable, key=lambda s: s.after_hours)
    if step.ratio <= 0.0:
        return None
    return LatePenalty(
        ratio=step.ratio,
        hours_late=round(hours, 4),
        due_at=due_at,
        submitted_at=submitted_at,
        reason=(
            f"締切を {hours:.1f} 時間超えています"
            f"（{step.after_hours:.0f} 時間超の段。総合点から {step.ratio:.0%} を引きます）"
        ),
    )


class HumanReview(BaseModel):
    """教員が 1 件を読んで下した判断。GradingRun を上書きせず追記する。

    **これは「成績が確定した」記録ではない**（それは `Finalization`）。
    ここにあるのは「教員がこの提出を実際に読んだ」という事実であり、
    一致度の測定が証拠として使ってよい唯一の記録である。一括確定や
    締切経過による自動確定はこの記録を作らない ── 誰も読んでいないのに
    「教員が AI に同意した」と書けば、測れる一致度がその分だけ嘘になる
    （ADR 0005）。

    **根拠説明を必須にする。** 学習者には AI の判定が既に示されており、
    教員がそれを覆すなら理由が要る。覆さない場合も同じで、「確認した」
    だけでは学習者に何も返らない（設計原則 P4 の「すべての判定は根拠を
    持つ」を人間の判定にも適用する）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: HumanReviewId
    grading_run_id: GradingRunId
    grader_id: UserId
    # 変更した観点だけを持つ。触っていない観点は AI の判定に同意したという意味。
    adjusted_levels: dict[CriterionId, int] = Field(default_factory=dict)
    # 根拠説明。**必須。**
    comment: str = Field(min_length=MIN_JUSTIFICATION_LENGTH)
    # 対応したレビュー依頼。教員が自発的に見た場合は None。
    request_id: ReviewRequestId | None = None
    # 遅延の減点を免除したか。**教員が覆せない減点を作らない**（P5）。
    # 病欠や事前の延長の合意はシステムの外で起きるので、規則の側では拾えない。
    penalty_waived: bool = False
    reviewed_at: datetime

    @property
    def agreed(self) -> bool:
        """AI の採点をそのまま承認したか。κ の算出に使う。

        **減点の免除はここに含めない。** 免除は AI の判定への不同意ではなく
        事務上の措置で、含めると一致度が「教員が AI に反対した」と読める
        （ADR 0010 が Finalization と HumanReview を分けたのと同じ理由）。
        """
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
    # **空になりうる。** 全観点を人が採点する課題（画像提出など）では、
    # 機械は 1 件も点を付けない。そのとき空を拒むと、そういう課題を
    # そもそも採点に載せられない（`_check_unscored` が、理由の無い空を
    # 落とす役を引き受ける）。
    criterion_scores: tuple[CriterionScore, ...] = ()
    kc_outcomes: tuple[KcOutcome, ...] = ()
    score_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    routing: Routing
    feedback: str | None = None
    # 確定根拠の素案（#97）。**教員が書く前に置いておくもので、根拠そのもの
    # ではない。** 材料は AI が出した観点別の判定と根拠だけで、そこに無い
    # 理由を作文させてはならない（ADR 0009 §4）。
    #
    # **人採点のみの課題では空のまま**になる ── AI 評価が走らないので材料が
    # 無く、材料が無いのに素案を作れば、それは必ず作文になる。
    #
    # 採点時に 1 度だけ作って run に残す。確定画面を開くたびに作ると、開く
    # たびに待たされるうえ、同じ提出について毎回違う文が出る。S6 が止まって
    # いれば素案が無いだけで、確定は妨げない（設計原則 P2）。
    justification_draft: str | None = None
    # **機械が点を付けなかった観点を、理由ごとに分けて持つ。**
    #
    # 3 つとも「CriterionScore が無い」点では同じだが、そこから導く帰結が
    # 正反対になる。1 つのリストに混ぜると、読む側は理由を知らないまま
    # 「評価器が落ちた」用の帰結（重み再配分・保留・レビュー必須・自動確定の
    # 停止）を全部に当てることになり、**打ち切った観点の点が上がる**か、
    # **人が採点すべき観点が誰も見ないまま閉じる**（Issue #10）。
    #
    #                     重み再配分  総合点   レビュー必須  自動確定
    #   評価器が落ちた        する      保留        する       止める
    #   ゲートで打ち切った    しない    出す        しない     止めない
    #   人が採点する          しない    保留        する       止める
    #
    # 理由を値として添えるのではなく**リストを分ける。** 添える形だと、
    # 分岐を書き忘れた場所が静かに従来どおり動く ── まさにこの Issue で
    # 起きていた壊れ方と同じ形になる。分けておけば読み落としは型で出る。
    #
    # 既存の記録は `unscored_criteria` しか持たない。新しい 2 つの既定を
    # 空にしてあるので、過去の run はそのまま読めて意味も変わらない
    # （当時は「評価器が落ちた」しか存在しなかった）。

    # 評価器の失敗・S6 停止で採点できなかった観点。空でなければ点は暫定で、
    # routing は必ず REVIEW_REQUIRED になる。
    unscored_criteria: tuple[CriterionId, ...] = ()
    # 集約のゲート（AND）が上位の観点で 0% を見たので、以降を評価しなかった
    # 観点。**これは保留ではなく確定した 0% である。** 仕様どおりの動作なので
    # レビューも自動確定の停止も要らず、重みどおり 0% として総合点に入る。
    skipped_criteria: tuple[CriterionId, ...] = ()
    # 評価器を割り当てていない観点（人が採点する）。人の入力が入るまで点は
    # 定まらないので、保留してレビューへ回す。
    awaiting_human: tuple[CriterionId, ...] = ()
    # 遅延の減点。**評価には入っていない。** `score_ratio` は遅延を知らない
    # 評価そのもので、学習者に見せる最終点は `final_score()` が両方から作る。
    penalty: LatePenalty | None = None
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
        scored = {score.criterion_id for score in self.criterion_scores}
        seen: set[CriterionId] = set()
        for reason, ids in (
            ("unscored", self.unscored_criteria),
            ("skipped", self.skipped_criteria),
            ("awaiting_human", self.awaiting_human),
        ):
            for criterion_id in ids:
                # 同じ観点に 2 つの理由は付かない。付いていれば、どちらの
                # 帰結を当てるかが読む側次第になる（この分割の意味が消える）。
                if criterion_id in seen:
                    raise ValueError(
                        f"criterion {criterion_id!r} is listed under more than one reason"
                    )
                if criterion_id in scored:
                    raise ValueError(
                        f"criterion {criterion_id!r} has a score but is listed as {reason}"
                    )
                seen.add(criterion_id)

        if not self.criterion_scores and not self.awaiting_human and not self.unscored_criteria:
            # 点も理由も無い採点は、ただの空の記録である。
            #
            # **理由は 2 つある。** 人の採点を待っている（`awaiting_human`）か、
            # 機械がまだ／もう答えていない（`unscored_criteria`）か。後者だけの
            # 採点は実在する ── AI 観点しか持たない課題の決定的段階がそれで、
            # 全観点が未採点のまま AI 段階へ渡る（#80）。どちらも無いときだけ
            # 空の記録である。
            raise ValueError(
                "a run with no criterion score must say why: "
                "which criteria await a human, or which went unscored"
            )

        if self.is_provisional and self.routing is not Routing.REVIEW_REQUIRED:
            # 誰も見ていない観点がある採点を自動確定させない（設計原則 P5）。
            # **ゲートで打ち切った観点はここに含めない** ── 打ち切りは仕様
            # どおりの結果であって、人が見るべき異常ではない。
            raise ValueError("a run with unscored criteria must be routed to review")
        return self

    @property
    def needs_review(self) -> bool:
        return self.routing is Routing.REVIEW_REQUIRED

    @property
    def is_provisional(self) -> bool:
        """点がまだ定まっていないか ── 総合点を出さず、自動確定もしない。

        **ゲートで打ち切った観点（`skipped_criteria`）は暫定ではない。**
        0% は確定した結果で、待つべき入力も落ちた評価器も無い。
        """
        return bool(self.unscored_criteria or self.awaiting_human)

    @property
    def missing_criteria(self) -> tuple[CriterionId, ...]:
        """機械が点を付けなかった観点。理由は問わない。

        「機械の判定が無い」ことだけが問題になる場所（測定の標本から外す、
        画面に段階を出さない）はこれを読む。**帰結を決める場所は理由の側を
        読むこと。**
        """
        return self.unscored_criteria + self.skipped_criteria + self.awaiting_human


def aggregate(
    scores: tuple[CriterionScore, ...], *, zero_weight: float = 0.0
) -> tuple[float, float]:
    """観点別スコアを総合点と確信度に畳む。

    決定的評価が確定させた観点（conclusive）は重みどおりに効き、
    その観点について AI の判定は最初から入ってこない（呼び出し側が排除する）。
    総合の確信度は、確定していない観点の確信度の最小値とする。
    平均ではなく最小を採るのは、ひとつでも自信のない観点があれば
    その採点全体を人間が見るべきだから。

    `zero_weight` は、**点が付いていないが 0% として重みどおり数える**観点の
    重みの合計（ゲートで打ち切った観点、人の入力を待っている観点）。ここを
    渡さずに `renormalize` で埋めると、残りの観点の重みが 1.0 まで膨らんで
    **打ち切られた学習者の点が上がる**（Issue #10 の衝突 1）。重みの合計は
    スコアの分と合わせて 1.0 でなければならない ── 合わなければ、どこかの
    観点が計算から丸ごと抜けている。
    """
    if not scores and zero_weight <= 0.0:
        raise ValueError("cannot aggregate an empty score set")
    if zero_weight < 0.0:
        raise ValueError("zero_weight must not be negative")

    total_weight = sum(score.weight for score in scores) + zero_weight
    if abs(total_weight - 1.0) > 1e-6:
        raise ValueError(f"criterion weights must sum to 1.0, got {total_weight}")

    score_ratio = sum(score.weighted for score in scores)
    uncertain = [score.confidence for score in scores if not score.conclusive]
    confidence = min(uncertain) if uncertain else 1.0
    return round(score_ratio, 10), confidence


# 0% と見なす境目。丸め誤差で「ほぼ 0」を通さないための幅で、段階の
# `score_ratio` は 0.0 をそのまま持つので、これは誤差の吸収にしか効かない。
GATE_ZERO = 1e-9


def gate_skipped(
    criteria: tuple[RubricCriterion, ...],
    scores: tuple[CriterionScore, ...],
    aggregation: Aggregation,
) -> tuple[CriterionId, ...]:
    """AND ゲートで打ち切る観点。評価順の上から見て、最初の 0% より後ろ。

    **判定できないところで止める。** まだ点の付いていない観点に行き当たったら
    そこで走査をやめて何も打ち切らない ── その観点が 0% になるかどうかは
    あとの段階（AI 評価）で決まるので、先回りして後続を切ると、実際には
    通っていた学習者の観点が 0% で確定してしまう。

    OR では常に空。**宣言するまで何も変わらない。**
    """
    if aggregation is not Aggregation.AND:
        return ()

    by_criterion = {score.criterion_id: score for score in scores}
    for index, criterion in enumerate(criteria):
        score = by_criterion.get(criterion.id)
        if score is None:
            return ()
        if score.score_ratio <= GATE_ZERO:
            return tuple(later.id for later in criteria[index + 1 :])
    return ()


def renormalize(
    scores: tuple[CriterionScore, ...], *, target: float = 1.0
) -> tuple[CriterionScore, ...]:
    """**評価器が落ちた**とき、残った観点の重みを比例配分し直す。

    評価器が落ちた観点を 0 点にすると学習者に不当な不利益が出るし、
    満点にすると誰も見ていない観点に点を与えることになる。どちらも取らず、
    採点できた観点だけで暫定の点を出し、**必ず人間のレビューに回す**
    （呼び出し側の責務）。GradingRun には未採点の観点を記録する。

    **使ってよいのは `unscored_criteria` に対してだけ。** ゲートで打ち切った
    観点や人が採点する観点にこれを当てると、残りの重みが 1.0 まで膨らみ、
    その観点が最初から無かったのと同じ点になる ── 打ち切られた学習者の点が
    上がる（Issue #10）。そちらは `aggregate(..., zero_weight=...)` で
    重みどおり 0% として数える。

    `target` は配分先の合計。0% として数える観点が同じ採点に居るときは、
    **その分を除いた残り**へ配分する（1.0 まで膨らませると、0% の観点が
    最初から無かったのと同じ点になる）。
    """
    if not scores:
        raise ValueError("cannot renormalize an empty score set")
    if not 0.0 < target <= 1.0:
        raise ValueError("renormalize target must be in (0.0, 1.0]")
    total = sum(score.weight for score in scores)
    if total <= 0.0:
        raise ValueError("total weight must be positive")
    return tuple(
        score.model_copy(update={"weight": score.weight / total * target}) for score in scores
    )


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


class FinalScore(BaseModel):
    """学習者に示す成績。評価と減点を**分けたまま**持つ。

    合計だけを返すと、画面にも異議申立にも「何点引かれたのか」が出せない
    （P4 は人間の判定にも根拠を要求する）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 遅延を知らない評価点。教員が段階を直していればそれを反映した値。
    evaluation: float = Field(ge=0.0, le=1.0)
    # 実際に引いた量。免除されていれば 0。
    penalty_ratio: float = Field(ge=0.0, le=1.0)
    final: float = Field(ge=0.0, le=1.0)
    waived: bool = False

    @property
    def penalized(self) -> bool:
        return self.penalty_ratio > 0.0


def final_score(
    run: GradingRun,
    task_version: TaskVersion,
    review: HumanReview | None = None,
) -> FinalScore:
    """評価 → 教員の修正 → 遅延の減点、の順に畳む。

    **総合点を作る場所はここ 1 つだけにする。** 画面ごとに書くと、教員が
    段階を直したときに減点が落ちる経路と落ちない経路ができる。
    """
    evaluation = run.score_ratio
    if review is not None and not review.agreed:
        # 教員が段階を変えたなら、評価点もそれに従う。`run.score_ratio` は
        # AI の判定に基づく値なので、そのまま出すと修正が反映されない。
        evaluation = _reviewed_evaluation(run, task_version, review)

    waived = bool(review is not None and review.penalty_waived)
    penalty = 0.0 if (run.penalty is None or waived) else run.penalty.ratio
    return FinalScore(
        evaluation=round(evaluation, 10),
        penalty_ratio=penalty,
        final=round(min(1.0, max(0.0, evaluation - penalty)), 10),
        waived=waived,
    )


def _reviewed_evaluation(run: GradingRun, task_version: TaskVersion, review: HumanReview) -> float:
    """教員が段階を直したあとの評価点。

    **課題のルーブリックを基準に回す。`run.criterion_scores` ではない。**
    以前はスコアの側を回していたので、**機械が一度も採点しなかった観点に
    教員が入れた段階が、総合点に入らなかった** ── 監査記録
    （`HumanReview.adjusted_levels`）には残るのに、点には出ない。評価器を
    割り当てない観点（人が採点する）は定義上いつもこれに当たるので、
    直さなければ人手の採点が成績に乗らない（Issue #7）。

    重みの取り方は、段階が全観点そろったかどうかで変える。

    そろっていれば**ルーブリックの重み**で数える。これが本来の配分で、
    欠けが埋まった以上そのまま使える。そろっていなければ、機械が採点した
    観点の重み（評価器が落ちた採点では比例配分済み）で暫定を出す ── 埋まって
    いない観点の重みを混ぜると、合計が 1.0 を超えたり、入っていない観点を
    0 点として数えたりする。どちらも学習者に見せる前に人が埋めるべき状態で、
    その総合点は保留されている（`is_provisional`）。
    """
    by_criterion = {score.criterion_id: score for score in run.criterion_scores}

    levels: dict[CriterionId, int] = {}
    for criterion in task_version.criteria:
        score = by_criterion.get(criterion.id)
        if score is not None:
            levels[criterion.id] = review.level_for(criterion.id, score.level)
        elif criterion.id in review.adjusted_levels:
            levels[criterion.id] = review.adjusted_levels[criterion.id]

    complete = len(levels) == len(task_version.criteria)

    total = 0.0
    for criterion in task_version.criteria:
        level = levels.get(criterion.id)
        if level is None:
            continue
        if complete:
            weight = criterion.weight
        else:
            score = by_criterion.get(criterion.id)
            if score is None:
                continue
            weight = score.weight
        total += criterion.level_for(level).score_ratio * weight
    return min(1.0, max(0.0, total))


def penalty_crosses_boundary(score: FinalScore, boundary: float | None) -> bool:
    """減点だけで合否が入れ替わるか。

    入れ替わるなら自動で閉じない ── 評価としては及第だったものが事務上の
    理由で不可になる件は、**教員が見てから確定させる**（P5）。遅延の事実に
    迷いは無いが、免除するかどうかの判断は人にしか無い。
    """
    if boundary is None or not score.penalized:
        return False
    return score.evaluation >= boundary > score.final
