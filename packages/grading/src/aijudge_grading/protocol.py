"""Evaluator プラグインの契約（ADR 0002）。

採点エンジンが Evaluator について知ってよいのはこの型だけ。
「コードか数式かレポートか」はエンジンに漏らさない。

Evaluator は Python の entry point（グループ `aijudge.evaluators`）として
登録する。新しい科目を足す作業が「パッケージを 1 つ足して YAML に名前を書く」
だけで済むことが、この設計の検証項目そのものになっている。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    RubricCriterion,
    Submission,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import ArtifactId


class EvaluationRequest(BaseModel):
    """Evaluator への入力。

    `prior_results` に決定的評価の結果を入れて AI 評価器へ渡すのが精度の鍵
    （設計方針 §04 step 3）。決定的評価器に対しては常に空。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    task_version: TaskVersion
    submission: Submission
    # Artifact の中身。ストレージアクセスは Evaluator の責務にしない。
    artifact_contents: dict[ArtifactId, bytes] = Field(default_factory=dict)
    # AI 評価器はルーブリック観点 1 つにつき 1 回呼ばれる。決定的評価器では None。
    criterion: RubricCriterion | None = None
    test_cases: tuple[TestCase, ...] = ()
    prior_results: tuple[CriterionScore, ...] = ()
    timeout_seconds: float = Field(default=10.0, gt=0.0)


class EvaluationOutcome(BaseModel):
    """Evaluator からの出力。

    失敗は例外ではなく結果として返す。1 つの評価器の失敗で
    採点全体を落とさないため（設計方針 §04 step 2）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EvaluatorStatus = EvaluatorStatus.OK
    scores: tuple[CriterionScore, ...] = ()
    raw_output: dict[str, object] = Field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class Evaluator(Protocol):
    """採点プラグイン。

    `evaluator_id` は科目プロファイル（subjects/*.yaml）から参照される名前。
    `kind` が DETERMINISTIC なら AI より先に走り、その判定は AI に覆されない。
    """

    evaluator_id: str
    kind: EvaluatorKind

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome: ...
