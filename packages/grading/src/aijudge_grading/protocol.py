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
    Artifact,
    ArtifactKind,
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
    # 科目プロファイルの evaluator_options から渡す評価器固有の設定。
    # コアの語彙を汚さずに「この科目ではサンプル数を 5 にする」を表現するため。
    options: dict[str, object] = Field(default_factory=dict)


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
    # 再現性のための出所（設計原則 P8）。パイプラインが GradingContext に集約する。
    model_id: str | None = None
    prompt_id: str | None = None


@runtime_checkable
class Evaluator(Protocol):
    """採点プラグイン。

    `evaluator_id` は科目プロファイル（subjects/*.yaml）から参照される名前。
    `kind` が DETERMINISTIC なら AI より先に走り、その判定は AI に覆されない。
    """

    evaluator_id: str
    kind: EvaluatorKind

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome: ...


@runtime_checkable
class Normalizer(Protocol):
    """提出物を評価器が読める形に直すプラグイン（設計方針 §4 の step 1）。

    評価器の前に走る。**ここで変換しておかないと、同じ変換を評価器ごとに
    書くことになる** ── レポート課題では構造チェッカーと AI 評価器の両方が
    同じ本文を要る。PDF を 2 回開くのは無駄なだけでなく、2 つの実装が
    食い違えば「構造は満たすのに AI には空に見える」が起きる。

    entry point のグループは `aijudge.normalizers`。科目プロファイルの
    `normalizers` が名前で指名する（評価器と同じ仕組み、ADR 0002）。

    **失敗は例外にしない。** 1 件の壊れた PDF で全員の採点を止めない。
    変換できなければ元の内容をそのまま返し、下流が「読めない」と判定する。
    """

    normalizer_id: str

    def applies_to(self, kind: ArtifactKind) -> bool:
        """この種類の提出物を扱うか。"""
        ...

    def normalize(self, artifact: Artifact, payload: bytes) -> bytes:
        """変換後の内容を返す。変換できなければ `payload` をそのまま返す。"""
        ...
