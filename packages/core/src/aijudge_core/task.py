"""課題（Task）とルーブリック。

TaskVersion を不変にしているのは採点の再現性（P8）のため。
問題文やルーブリックが直った後に過去の採点を読み返しても、
そのとき何を基準に採点したのかが必ず分かる。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import CourseId, CriterionId, TaskId, TaskVersionId, UserId
from .knowledge import QMatrixEntry


class ReviewState(StrEnum):
    """教員レビューの状態。AI 生成問題も手動作成問題も同じ状態機械を通る。"""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Provenance(BaseModel):
    """この TaskVersion が誰の・何の産物か。

    AI 生成であることを隠さない。採点側はこれを見ないが（P1）、
    教員向けの表示と、生成品質の統計（PoC-2 の承認率）に必要。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authored_by: UserId | None = None
    generated_by: str | None = None
    generation_prompt_version: str | None = None
    review_state: ReviewState = ReviewState.DRAFT
    reviewed_by: UserId | None = None
    reject_reason: str | None = None

    @model_validator(mode="after")
    def _check_origin(self) -> Self:
        if self.authored_by is None and self.generated_by is None:
            raise ValueError("Provenance requires either authored_by or generated_by")
        if self.review_state is ReviewState.REJECTED and not self.reject_reason:
            # 却下理由は作問モデル改善の学習データになるので必須にする。
            raise ValueError("a rejected TaskVersion must carry a reject_reason")
        return self


class RubricLevel(BaseModel):
    """観点内の到達段階。`score_ratio` はその観点の配点に対する比率。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: int = Field(ge=0)
    label: str = Field(min_length=1)
    descriptor: str = Field(min_length=1)
    score_ratio: float = Field(ge=0.0, le=1.0)


class RubricCriterion(BaseModel):
    """ルーブリックの 1 観点。AI 評価器はこの単位で 1 回呼ばれる（§04）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CriterionId
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: float = Field(gt=0.0, le=1.0)
    levels: tuple[RubricLevel, ...] = Field(min_length=2)
    evaluator_id: str | None = None

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        levels = [level.level for level in self.levels]
        if len(set(levels)) != len(levels):
            raise ValueError("RubricLevel.level must be unique within a criterion")
        if sorted(levels) != levels:
            raise ValueError("RubricLevel entries must be ordered by level")
        if max(level.score_ratio for level in self.levels) != 1.0:
            raise ValueError("the top RubricLevel must have score_ratio 1.0")
        return self

    def level_for(self, level: int) -> RubricLevel:
        for candidate in self.levels:
            if candidate.level == level:
                return candidate
        raise KeyError(f"no level {level} in criterion {self.code!r}")


class TestCase(BaseModel):
    """決定的評価器が使う検証データ。中身の解釈は Evaluator に任せる（P1）。

    コードのテストケースも、数式の同値判定に使う参照式も、
    レポートの必須節リストも、すべてこの型に載せる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    evaluator_id: str = Field(min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)
    hidden: bool = True
    weight: float = Field(default=1.0, gt=0.0)


class TaskVersion(BaseModel):
    """課題の 1 版。公開後は不変。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TaskVersionId
    task_id: TaskId
    version: int = Field(ge=1)
    subject_profile: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    reference_solution: str | None = None
    criteria: tuple[RubricCriterion, ...] = Field(min_length=1)
    test_cases: tuple[TestCase, ...] = ()
    q_matrix: tuple[QMatrixEntry, ...] = ()
    max_score: float = Field(gt=0.0)
    allow_handwriting: bool = False
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def _check_weights(self) -> Self:
        total = sum(criterion.weight for criterion in self.criteria)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"rubric weights must sum to 1.0, got {total}")
        codes = [criterion.code for criterion in self.criteria]
        if len(set(codes)) != len(codes):
            raise ValueError("RubricCriterion.code must be unique within a TaskVersion")
        for entry in self.q_matrix:
            if entry.task_version_id != self.id:
                raise ValueError("QMatrixEntry.task_version_id must match the TaskVersion")
        return self

    @property
    def is_published(self) -> bool:
        return self.provenance.review_state is ReviewState.APPROVED

    def criterion(self, criterion_id: CriterionId) -> RubricCriterion:
        for criterion in self.criteria:
            if criterion.id == criterion_id:
                return criterion
        raise KeyError(f"no criterion {criterion_id!r} in {self.id!r}")


class Task(BaseModel):
    """課題そのもの。版をまたいだ同一性を担う。

    `unit` と `session` は課題を「何回目の課題か」でまとめるためにある。
    1 回の授業で複数問（`p1 p2 p3`）出るので、一覧を平らに並べると
    学習者も教員も何回目の分を見ているのか分からなくなる。

    `unit` は取り込み元のまとまりの名前（`ex03` など）で、同一性の鍵。
    `session` は並べ替え用の数値。`unit` から機械的に取れないことがある
    （`exam08` のような名前）ので別に持つ。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TaskId
    course_id: CourseId
    title: str = Field(min_length=1)
    # 何回目のまとまりか（例: "ex03"）。取り込み元のディレクトリ名。
    unit: str | None = None
    # 何回目か。並べ替えに使う。`unit` から取れないこともある。
    session: int | None = Field(default=None, ge=1)
    # まとまりの中での順序（p1, p2, … の 1, 2, …）。
    position: int | None = Field(default=None, ge=1)
    current_version_id: TaskVersionId | None = None
    # 提示日時。学習者に見せる「何日提示の課題か」がこれ。
    opens_at: datetime | None = None
    due_at: datetime | None = None

    @property
    def unit_label(self) -> str:
        """まとまりの表示名。

        `session` があれば「第 3 回」、無ければ `unit` をそのまま出す
        （`exam08` のような、回に対応しないまとまりがある）。
        """
        if self.session is not None:
            return f"第 {self.session} 回"
        return self.unit or "未分類"

    @property
    def sort_key(self) -> tuple[int, str, int]:
        """一覧の並び順。回がある課題を先に、無いものを後に。"""
        return (
            self.session if self.session is not None else 10**6,
            self.unit or "",
            self.position if self.position is not None else 10**6,
        )
