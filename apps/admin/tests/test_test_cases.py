"""課題文にテストケースを付ける生成の規則を固定する。

**参照解答を必ず一緒に作らせる。** 期待出力を作るには課題を解く必要があり、
それが正しいかを確かめる手段は「参照解答が自分のテストケースを全部通るか」
（門 1）しか無い。片方だけ作らせると照合する相手が無いまま期待出力が決定的
採点に入り、決定的な結果は `conclusive` なので AI に見直されない（P3）。
期待出力が 1 件でも違えば、その課題は全員が減点され、原因は提出物の側に見える。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aijudge_admin.test_cases import PROMPT, GeneratedCases, TestCaseWriter
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

_PAYLOAD = {
    "reference_solution": "int main(void){return 0;}",
    "test_cases": [
        {"name": "case1", "input": "1 2", "expected": "3"},
        {"name": "case2", "input": "2 3", "expected": "5"},
    ],
}


def _writer(payload=None):
    provider = ScriptedProvider([json.dumps(payload or _PAYLOAD)])
    return TestCaseWriter(LlmGateway(provider), model="test"), provider


def test_the_statement_reaches_the_prompt() -> None:
    writer, provider = _writer()
    writer.write("2 つの整数を読み、和を出力しなさい。", language="c", count=2)
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "2 つの整数を読み、和を出力しなさい。" in sent
    assert "c" in sent


def test_a_reference_solution_always_comes_with_the_cases() -> None:
    """**片方だけでは門 1 を通せない。** 型で要求する。"""
    with pytest.raises(ValidationError):
        GeneratedCases.model_validate({"test_cases": _PAYLOAD["test_cases"]})
    with pytest.raises(ValidationError):
        GeneratedCases.model_validate({"reference_solution": "int main(void){}"})


def test_two_cases_are_the_minimum() -> None:
    """1 件では、どの入力でも同じ出力を返す解答を弾けない。"""
    with pytest.raises(ValidationError):
        GeneratedCases.model_validate(
            {
                "reference_solution": "int main(void){}",
                "test_cases": [{"name": "only", "input": "1", "expected": "1"}],
            }
        )


def test_the_prompt_forbids_rewriting_the_statement() -> None:
    """課題文は教員が書いたもので、そのまま出題される。"""
    assert "課題文を書き換えません" in (PROMPT.system or "")


def test_the_prompt_pins_the_output_format_to_the_statement() -> None:
    """**書式を勝手に決めさせない。** 決めると正しい提出が不正解になる。

    区切り方の食い違いは全ケースを同時に落とす（`code_test_runner` の
    `normalize_output` は書式を課題の一部として扱う）。
    """
    system = PROMPT.system or ""
    assert "課題文の指定にそのまま従います" in system


def test_the_result_records_what_made_it() -> None:
    """再現性のため（P8）。どの版のプロンプトとどのモデルが出したか。"""
    writer, _ = _writer()
    result = writer.write("課題文", count=2)
    assert result.prompt_id == "test_cases_for_statement_ja@1"
    assert result.model == "test"
    assert len(result.test_cases) == 2
