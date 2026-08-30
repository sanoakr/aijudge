"""AI 作問の語彙（S2、設計方針 §5）。

    Blueprint（KC + 難度 + 形式 + 制約）
      → TaskDraft（問題文・参照解答・テストケースを同時に生成）
      → 門 2 つ（`verification.py`）
      → 教員レビュー
      → 公開

**このモジュールは LLM を呼ばない。** 持つのは「何を頼むか」と「返ってきた
ものをどう受け取るか」だけで、呼ぶのは app 層である（S2 と S6 は互いを
import しない ── `.importlinter` の独立契約）。分けてあるので、生成物の
検査は実際のモデルなしで試験できる。

**生成物は手で作った課題と完全に同じ型に載る**（P1/P2）。採点側は生成物か
どうかを知らない。知る必要があるのは教員レビューの導線だけで、それは
`Provenance.generated_by` と `review_state` が担う。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .spec import TaskSpec, TestCaseSpec


class Difficulty(StrEnum):
    """狙う難度。**正答率の予測ではなく、作問の指示である。**

    実際の難度は出題してみるまで分からない（Phase 4 の難度推定は過去の
    正答率から当てる別の仕事）。ここにあるのは「どのくらいのつもりで作るか」。
    """

    INTRODUCTORY = "introductory"
    STANDARD = "standard"
    CHALLENGING = "challenging"


class Blueprint(BaseModel):
    """1 つの課題を作る指示。

    **KC を必ず持つ。** 何を問う課題か決めずに生成させると、既存の課題と
    見分けの付かないものが出る。KC は Q-matrix の語彙そのものなので、
    生成・採点・習熟度推定が同じ言葉で繋がる（設計原則 P6）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 狙う KC の正準キー。**空にしない。**
    knowledge_components: tuple[str, ...] = Field(min_length=1)
    subject_profile: str = Field(min_length=1)
    # このコースの素性（題名と、シラバスの概要・到達目標）。**任意。**
    #
    # KC は「何を問うか」を決めるが、「どこまでを既習として書いてよいか」は
    # 決めない。同じ `cs.loops` でも、入門の第 3 回と専門科目とでは前提に
    # できるものが違う。到達目標を渡すと、その範囲の外に出た課題文が減る。
    #
    # **文字列で受ける。** `Course` を受け取ると作問が core の語彙に依存し、
    # 「Evaluator と作問はコースを知らない」という境界（ADR 0001）が崩れる。
    course_title: str = ""
    course_outline: str = ""
    difficulty: Difficulty = Difficulty.STANDARD
    language: str = Field(default="c", min_length=1)
    # 課題文に必ず書かせたい制約（入出力の形式、禁止する標準ライブラリなど）。
    constraints: tuple[str, ...] = ()
    # 既存の課題と似せないための材料。**既存問題の本文をそのまま渡す**ので、
    # 学習者のデータは含まない（P7 の観点で、ここは外部モデルにも渡せる）。
    avoid_similar_to: tuple[str, ...] = ()
    test_case_count: int = Field(default=5, ge=2, le=20)

    @model_validator(mode="after")
    def _check(self) -> Blueprint:
        if len(set(self.knowledge_components)) != len(self.knowledge_components):
            raise ValueError("KC の指定が重複しています")
        return self


class DraftTestCase(BaseModel):
    """生成されたテストケース 1 件。"""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=64)
    input: str = Field(max_length=8000)
    expected: str = Field(max_length=8000)


class TaskDraft(BaseModel):
    """モデルに返させる構造化出力（設計原則 P4）。

    **参照解答とテストケースを同時に出させる。** 別々に頼むと、片方だけが
    もっともらしい組が出てくる ── 門 1（参照解答が全ケースを通る）は、
    同時に作られたものでなければまず通らない。通らないことがすぐ分かるのが
    利点で、**門が生成の品質を測る道具になる**。
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=8000)
    reference_solution: str = Field(min_length=1, max_length=20000)
    test_cases: tuple[DraftTestCase, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _check(self) -> TaskDraft:
        names = [case.name for case in self.test_cases]
        if len(set(names)) != len(names):
            raise ValueError("テストケース名が重複しています")
        return self


def draft_to_spec(
    draft: TaskDraft,
    blueprint: Blueprint,
    *,
    key: str,
    evaluator: str = "code_test_runner",
    readability_weight: float = 0.0,
) -> TaskSpec:
    """生成物を、手で作った課題と同じ型に落とす。

    **生成専用の経路を作らない**（P1/P2）。ここを通ったあとは、課題が
    どこから来たかを採点側は知らない。知る必要があるのは教員レビューの
    導線だけで、それは `Provenance` が持つ。
    """
    return TaskSpec(
        key=key,
        title=draft.title,
        statement=draft.statement,
        reference_solution=draft.reference_solution,
        evaluator=evaluator,
        readability_weight=readability_weight,
        knowledge_components=blueprint.knowledge_components,
        test_cases=tuple(
            TestCaseSpec(name=case.name, input=case.input, expected=case.expected)
            for case in draft.test_cases
        ),
    )


__all__ = [
    "Blueprint",
    "Difficulty",
    "DraftTestCase",
    "TaskDraft",
    "draft_to_spec",
]
