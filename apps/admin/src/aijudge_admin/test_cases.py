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


__all__ = ["PROMPT", "GeneratedCases", "GenerationResult", "TestCaseWriter"]
