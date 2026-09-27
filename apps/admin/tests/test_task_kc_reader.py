"""課題 1 問の知識要素を選ばせる読み手（#496）。

以前はシラバス用の読み手を流用していて、当てはまる課題でも候補が 0 件になった
（prog2 ex2「整数の正負」── コースに「分岐」があるのに）。原因は返り値の型が
空の `{}` を許していたこと。ここで固定するのは 4 つ。

空は通さない   `{}` は正しい答えではない。やり直させ、それでも駄目なら失敗にする
               （「候補 0 件」として黙って通さない）。
材料が届く     名前・参照解答・いま付いているものが指示文に入る。
関門は 1 つ     一覧（コースの知識要素）に無いキーは理由を添えて落とす。
削除は付いているものだけ  付いていないものを外せとは言わない。
"""

from __future__ import annotations

import json

import pytest

from aijudge_course_admin.syllabus import (
    MAX_TASK_KCS,
    TASK_KC_PROMPT,
    TaskKcReader,
    TaskKcSelection,
)
from aijudge_llm_gateway import LlmGateway, ScriptedProvider, StructuredOutputError

VOCABULARY = {
    "cs.sdf.fundamentals.branching": "分岐",
    "cs.sdf.fundamentals.console_io": "コンソール入出力",
    "cs.fpl.translation.compile_link": "コンパイルとリンク",
}
STATEMENT = "キーボードから入力した整数値の正負を判定し、正・負・0 を出力しなさい。"
SOLUTION = 'int main(void){int n; scanf("%d",&n); if(n>0) puts("正"); return 0;}'


def _select(*answers: dict | str, current=("cs.fpl.translation.compile_link",)):
    provider = ScriptedProvider([a if isinstance(a, str) else json.dumps(a) for a in answers])
    reader = TaskKcReader(LlmGateway(provider), model="test")
    result = reader.select(
        STATEMENT, vocabulary=VOCABULARY, current=current, reference_solution=SOLUTION
    )
    return result, provider


def test_an_empty_object_is_not_an_answer() -> None:
    """`{}` は Schema で弾かれる。既定値が無いので、両方の欄が必須である。"""
    assert set(TaskKcSelection.model_json_schema()["required"]) == {
        "used",
        "not_used_among_current",
    }
    with pytest.raises(StructuredOutputError):
        _select("{}", "{}", "{}")


def test_the_names_the_solution_and_the_current_ones_reach_the_prompt() -> None:
    _result, provider = _select({"used": [], "not_used_among_current": []})
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "cs.sdf.fundamentals.branching — 分岐" in sent, "キーだけで名前が無い"
    assert 'scanf("%d",&n)' in sent, "参照解答が届いていない"
    assert "- cs.fpl.translation.compile_link" in sent, "いま付いているものが届いていない"


def test_additions_come_with_evidence_and_removals_with_reasons() -> None:
    result, _ = _select(
        {
            "used": [
                {"key": "cs.sdf.fundamentals.branching", "evidence": "if (n>0) ... else ..."},
                {"key": "cs.sdf.fundamentals.console_io", "evidence": "scanf と puts"},
            ],
            "not_used_among_current": [
                {"key": "cs.fpl.translation.compile_link", "reason": "入出力と分岐だけ"}
            ],
        }
    )
    assert [(u.key, u.evidence) for u in result.add] == [
        ("cs.sdf.fundamentals.branching", "if (n>0) ... else ..."),
        ("cs.sdf.fundamentals.console_io", "scanf と puts"),
    ]
    assert [(r.key, r.reason) for r in result.remove] == [
        ("cs.fpl.translation.compile_link", "入出力と分岐だけ")
    ]
    assert result.prompt_id == TASK_KC_PROMPT.id


def test_a_key_outside_the_list_is_dropped_with_its_reason() -> None:
    result, _ = _select(
        {
            "used": [
                {"key": "cs.sdf.fundamentals.branching", "evidence": "if"},
                {"key": "cs.sdf.fundamentals.loops", "evidence": "無い"},
                {"key": "分岐", "evidence": "日本語のキー"},
            ],
            "not_used_among_current": [],
        }
    )
    assert [u.key for u in result.add] == ["cs.sdf.fundamentals.branching"]
    assert [d.key for d in result.discarded] == ["cs.sdf.fundamentals.loops", "分岐"]
    assert all(d.reason for d in result.discarded)


def test_only_what_is_attached_can_be_suggested_for_removal() -> None:
    result, _ = _select(
        {
            "used": [],
            "not_used_among_current": [
                {"key": "cs.sdf.fundamentals.console_io", "reason": "付いていない"},
                {"key": "cs.fpl.translation.compile_link", "reason": "使わない"},
            ],
        }
    )
    assert [r.key for r in result.remove] == ["cs.fpl.translation.compile_link"]


def test_what_is_used_is_not_also_suggested_for_removal() -> None:
    """両方の欄に同じものを写す答えを実測した（運用機・gemma4:e4b・ex01-1）。"""
    result, _ = _select(
        {
            "used": [{"key": "cs.fpl.translation.compile_link", "evidence": "cc でビルドする"}],
            "not_used_among_current": [
                {"key": "cs.fpl.translation.compile_link", "reason": "写しただけ"}
            ],
        }
    )
    assert [u.key for u in result.add] == ["cs.fpl.translation.compile_link"]
    assert result.remove == ()


def test_duplicates_are_folded_and_the_list_is_capped() -> None:
    used = [{"key": "cs.sdf.fundamentals.branching", "evidence": str(i)} for i in range(3)]
    many = {f"cs.sdf.fundamentals.k{i}": f"要素{i}" for i in range(MAX_TASK_KCS + 3)}
    provider = ScriptedProvider(
        [
            json.dumps({"used": used, "not_used_among_current": []}),
            json.dumps(
                {
                    "used": [{"key": k, "evidence": "e"} for k in many],
                    "not_used_among_current": [],
                }
            ),
        ]
    )
    reader = TaskKcReader(LlmGateway(provider), model="test")
    folded = reader.select(STATEMENT, vocabulary=VOCABULARY)
    capped = reader.select(STATEMENT, vocabulary=many)
    assert [u.key for u in folded.add] == ["cs.sdf.fundamentals.branching"]
    assert len(capped.add) == MAX_TASK_KCS


def test_the_prompt_asks_not_to_list_boilerplate() -> None:
    """`return 0;` から「戻り値」を拾う雑音を実測した（運用機・gemma4:e4b）。"""
    assert "どの課題にも現れる定型は挙げません" in TASK_KC_PROMPT.template
