"""課題文からテストケースを作る（参照解答と一緒に）。

**参照解答を必ず一緒に作らせる。** テストケースの期待出力を作るには課題を
解く必要があり、解いた結果が正しいかを確かめる手段は「参照解答が自分の
テストケースを全部通るか」（門 1）しか無い。片方だけ作らせると、照合する
相手が無いまま期待出力が決定的採点に入る ── そして決定的な結果は
`conclusive` なので AI に見直されない（設計原則 P3）。期待出力が 1 件でも
間違っていれば、**その課題は全員が減点され、原因は提出物の側に見える。**

`TaskDrafter` が「問題文・参照解答・テストケースを必ず同時に作る」と指示して
いるのと同じ理由である（ADR 0008 が「門が生成の品質を測る道具になる」と
書いているのはこの性質を指す）。違うのは、こちらは**問題文が既にある**こと
だけで、作らせるのは残りの 2 つ。

**門を通ったことは承認の代わりにならない。** 門が言うのは「参照解答と
テストケースが整合している」までで、それが**問題文の意図と合っているか**は
見ていない（`solvability` が「解けなかったことは却下の理由ではない」と
言っているのと同じ限界）。だから生成したものは承認待ちで保存する（P5）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from aijudge_authoring.drafting import DraftTestCase
from aijudge_llm_gateway import (
    DataClass,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)


class GeneratedCases(BaseModel):
    """モデルに返させる構造化出力（設計原則 P4）。"""

    model_config = ConfigDict(extra="ignore")

    reference_solution: str = Field(min_length=1, max_length=20000)
    test_cases: tuple[DraftTestCase, ...] = Field(min_length=2)


PROMPT = PromptTemplate(
    name="test_cases_for_statement_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは大学の理工系科目の課題に、参照解答とテストケースを付ける教員です。"
        "**課題文を書き換えません。** 与えられた課題文がそのまま出題されます。"
        "**参照解答とテストケースを必ず同時に作ります。** 参照解答はすべての"
        "テストケースを通らなければなりません。"
        "テストケースは入力ごとに出力が変わるものにします ── どの入力でも同じ"
        "出力になる組は、解答の中身を確かめられません。"
        "**出力の形式は課題文の指定にそのまま従います。** 区切り文字・桁数・"
        "改行の位置を勝手に決めると、正しい提出が不正解になります。"
    ),
    template=(
        "## 課題文\n{statement}\n\n"
        "## 言語\n{language}\n\n"
        "## テストケース数\n{count} 件。"
        "うち少なくとも 1 件は境界値（最小の入力、値が等しい場合など）にすること。\n"
    ),
)


@dataclass(frozen=True)
class GenerationResult:
    """生成物と、それがどう作られたか（再現性のため・P8）。"""

    reference_solution: str
    test_cases: tuple[DraftTestCase, ...]
    prompt_id: str
    model: str


class TestCaseWriter:
    """課題文にテストケースを付ける。**採点そのものには関与しない。**"""

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        # **4096 では足りない課題がある。** 期待出力がそのまま入るので、
        # 「1 から 100 まで出力する」ような課題では 1 ケースが 100 行になる。
        # 足りないと応答が途中で切れ、返るのは「JSON として読めない」という
        # 形式の誤りだけで、**長さが理由だと画面から読めない**（実測: 4096 で
        # 15600 文字目で切断）。
        max_tokens: int = 8192,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def write(self, statement: str, *, language: str = "c", count: int = 5) -> GenerationResult:
        result = self._gateway.complete_structured(
            PROMPT,
            GeneratedCases,
            model=self._model,
            # 課題文は教員が書いたもので、学習者のデータを含まない（P7）。
            data_class=DataClass.NON_PERSONAL,
            # **既定の 120 秒では足りない。** 参照解答 1 本とテストケース数件を
            # 一度に書かせるので、実測 85 秒（gemma4:e4b・小さな課題・温まった
            # 状態）。モデルの読み込みから始まればこれを超え、そのとき返るのは
            # 「タイムアウト」だけで、何が起きたのか画面から読めない。
            timeout_seconds=300.0,
            max_tokens=self._max_tokens,
            statement=statement[:8000],
            language=language,
            count=count,
        )
        return GenerationResult(
            reference_solution=result.value.reference_solution,
            test_cases=result.value.test_cases,
            prompt_id=PROMPT.id,
            model=self._model,
        )


# --------------------------------------------------------------------------
# 段階を踏んで作る（#305）
# --------------------------------------------------------------------------
#
# `TestCaseWriter` は参照解答とテストケースを 1 回でまとめて作る。**それとは
# 別に、人が途中で確かめられる経路を置く。** 解答例を読んで直してから、その
# 解答例を使ってテストケースを提案させる ── まとめて作ると、教員が受け取る
# のは「どこから来たのか分からない期待出力」になる。
#
# **期待出力はモデルに書かせない。** 参照解答を実際に走らせた出力を使う
# （`outputs_for`）。誤った期待出力は「全員が落ちる」として現れ、原因は提出物の
# 側に見える ── 決定的な結果は `conclusive` なので AI にも見直されない（P3）。


class WrittenSolution(BaseModel):
    """解答例だけを書かせるときの構造化出力（P4）。"""

    model_config = ConfigDict(extra="ignore")

    solution: str = Field(min_length=1, max_length=20000)


class ProposedInput(BaseModel):
    """提案されたテストケース 1 件。**入力だけ。**

    期待出力は参照解答を走らせて埋めるので、モデルには書かせない。
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=64)
    input: str = Field(max_length=8000)
    why: str = Field(default="", max_length=200)


class ProposedInputs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cases: tuple[ProposedInput, ...] = Field(min_length=1)


SOLUTION_PROMPT = PromptTemplate(
    name="reference_solution_for_statement_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは大学の理工系科目の課題に、参照解答を書く教員です。"
        "**課題文を書き換えません。** 与えられた課題文がそのまま出題されます。"
        "**出力の形式は課題文の指定にそのまま従います** ── 区切り文字・桁数・"
        "改行の位置を勝手に決めると、この解答例から作るテストケースが"
        "正しい提出を不正解にします。"
        "標準入力から読み、標準出力へ書きます。"
        "JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template=(
        "## 課題文\n{statement}\n\n"
        "## 言語\n{language}\n\n"
        '出力する JSON の形: {{"solution": "ソースコード全体"}}\n'
    ),
)

INPUTS_PROMPT = PromptTemplate(
    name="test_case_inputs_for_solution_ja",
    version="1",
    system=(
        "あなたは大学の理工系科目の課題に、テストケースの入力を考える教員です。"
        "**入力だけを作ります。期待出力は書きません** ── 期待出力は参照解答を"
        "実際に走らせて埋めるので、書いても使われません。"
        "入力ごとに出力が変わるものにします ── どの入力でも同じ出力になる組は、"
        "解答の中身を確かめられません。"
        "少なくとも 1 件は境界値（最小の入力、値が等しい場合など）にします。"
        "JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template=(
        "## 課題文\n{statement}\n\n"
        "## 参照解答（{language}）\n```\n{reference}\n```\n\n"
        "## 作る件数\n{count} 件\n\n"
        "出力する JSON の形: "
        '{{"cases": [{{"name": "case1", "input": "標準入力に流す内容", '
        '"why": "何を確かめるか"}}]}}\n'
    ),
)


@dataclass(frozen=True)
class SolutionResult:
    """書かせた解答例と、その出所（P8）。"""

    solution: str
    prompt_id: str
    model: str


@dataclass(frozen=True)
class InputsResult:
    """提案された入力と、その出所（P8）。"""

    cases: tuple[ProposedInput, ...]
    prompt_id: str
    model: str


class SolutionWriter:
    """課題文から解答例を書く。**保存はしない。**

    書いたものは教員が読んで直す前提である ── モデルの解答が課題文の意図と
    合っているかは、門では確かめられない（`TaskVerifier` が言えるのは
    「参照解答とテストケースが整合している」までで、両方が同じ勘違いを
    していればそのまま通る）。
    """

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def write(self, statement: str, *, language: str = "c") -> SolutionResult:
        result = self._gateway.complete_structured(
            SOLUTION_PROMPT,
            WrittenSolution,
            model=self._model,
            # 課題文は教員が書いたもので、学習者のデータを含まない（P7）。
            data_class=DataClass.NON_PERSONAL,
            timeout_seconds=300.0,
            max_tokens=self._max_tokens,
            statement=statement[:8000],
            language=language,
        )
        return SolutionResult(
            solution=result.value.solution,
            prompt_id=SOLUTION_PROMPT.id,
            model=self._model,
        )


class InputProposer:
    """解答例と課題文から、テストケースの**入力**を提案する。

    **期待出力は返さない。** 参照解答を走らせて埋める（`outputs_for`）ので、
    モデルが書いた出力は使わない ── 誤った期待出力は全員の減点として現れ、
    原因が提出物の側に見える。
    """

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def propose(
        self, statement: str, reference: str, *, language: str = "c", count: int = 5
    ) -> InputsResult:
        result = self._gateway.complete_structured(
            INPUTS_PROMPT,
            ProposedInputs,
            model=self._model,
            data_class=DataClass.NON_PERSONAL,
            timeout_seconds=300.0,
            max_tokens=self._max_tokens,
            statement=statement[:8000],
            reference=reference[:8000],
            language=language,
            count=count,
        )
        return InputsResult(
            cases=result.value.cases,
            prompt_id=INPUTS_PROMPT.id,
            model=self._model,
        )


__all__ = [
    "INPUTS_PROMPT",
    "PROMPT",
    "SOLUTION_PROMPT",
    "GeneratedCases",
    "GenerationResult",
    "InputProposer",
    "InputsResult",
    "ProposedInput",
    "ProposedInputs",
    "SolutionResult",
    "SolutionWriter",
    "TestCaseWriter",
    "WrittenSolution",
]
