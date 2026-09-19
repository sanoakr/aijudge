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
    # 提出者を**人が識別する記号**（学籍番号・ログイン ID）。`Submission` が
    # 持つのは内部 ID だけなので、これが要る観点は他に手が無い。
    #
    # **要るのは「提出物に本人の学籍番号が書いてあるか」を見る観点である。**
    # 実例: paiza の認定証のニックネームに学籍番号を入れさせる課題
    # （`image_text_check`）。これは提出物の中身と提出者の対応を見る採点で
    # あって、締切や遅延のような運用値ではない ── だから ADR 0013 が
    # `due_at` を締め出したのとは扱いが違う。
    #
    # **解決するのは合成ルートである。** パイプラインは利用者表を引かない
    # （`aggregation` と同じ形）。埋まっていなければ、それを要る評価器は
    # 「満たした」と言わない ── 分からないまま通すと誰の認定証でも通る。
    learner_reference: str | None = None
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

    **`EvaluationRequest.test_cases` を読む評価器は `uses_test_cases = True`
    を宣言する**（#300）。契約の必須項目にはしない ── 既存の評価器と
    テストの替え玉が全部その属性を持つことになる。宣言しなければ
    「読まない」で、それが多数派である（`reads_test_cases`）。

    この宣言があるのは、**入出力セットの欄を出すかどうかを画面が決められる
    ようにする**ため。画面が評価器名の表を持つと、評価器を足した日に
    その表だけが古くなる（`_evaluator_rows` が説明を docstring から取るのと
    同じ理由）。
    """

    evaluator_id: str
    kind: EvaluatorKind

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome: ...


#: 検証データの形。画面はこれで編集の仕方を選ぶ（#302）。
#: `io` は入力と期待出力の組、`items` は項目の並び。
TEST_CASE_SHAPES = ("io", "items")


def test_case_shape(evaluator: object) -> str | None:
    """この評価器が読む検証データの形。

    **画面が評価器名の表を持たないため**にある（`reads_test_cases` と同じ
    理由）。入出力の編集欄と項目表の編集欄は別の画面部品なので、どちらを
    出すかは評価器に言わせる。宣言が無ければ入出力（従来の形）。

    知らない形を名乗る評価器には、画面は編集欄を出さずに「この評価器の
    検証データは画面から編集できません」と言う ── 知らない形を入出力の
    欄で編集させると、黙って壊す。
    """
    if not reads_test_cases(evaluator):
        return None
    shape = getattr(evaluator, "test_case_shape", "io")
    return str(shape)


def reads_test_cases(evaluator: object) -> bool:
    """この評価器は入出力セット（`EvaluationRequest.test_cases`）を読むか。

    宣言していなければ読まない。**「決定論的かどうか」とは別の問い**である。
    提出の遵守（`submission_compliance`）は決定論的だが検証データを持たず、
    項目表の照合（`checklist_ai_judge`）は AI だが課題ごとの項目表を読む。
    種別で代用すると、片方には永久に空の欄が出て、もう片方には欄が出ない。
    """
    return bool(getattr(evaluator, "uses_test_cases", False))


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
