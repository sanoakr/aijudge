"""確定根拠の素案（#97）。

**根拠そのものは作らせない**（ADR 0009 §4）。素案の材料は、その提出について
AI が出した観点別の判定と根拠だけである。そこに無い理由を作文させると、
教員が考えていない理由が学習者に出て、その記録が一致度の標本に混ざる。
"""

from __future__ import annotations

import json

import pytest

from aijudge_admin.justification import DRAFT_PROMPT, JustificationWriter
from aijudge_llm_gateway import LlmGateway, PolicyViolation, ScriptedProvider

JUDGEMENTS = "- 出力の正しさ: 達成（全ケース通過）\n- 変数名と構造: 概ね"


def _writer(text: str = "テストケース 3 は正しく通っています。"):
    provider = ScriptedProvider([json.dumps({"text": text})])
    return JustificationWriter(LlmGateway(provider), model="test"), provider


def test_the_ai_judgements_are_what_is_sent() -> None:
    """**材料は AI の判定。** 提出の中身でも教員の書いたものでもない。"""
    writer, provider = _writer()
    writer.draft(JUDGEMENTS)
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "出力の正しさ: 達成" in sent


def test_the_prompt_forbids_inventing_reasons() -> None:
    """**理由を推測して補わせない。** 素案は判定の言い換えに留める。"""
    assert "渡された判定に書かれていないことを足しません" in (DRAFT_PROMPT.system or "")


def test_the_prompt_leaves_the_score_to_the_instructor() -> None:
    """点の妥当性は論じさせない ── 決めるのは教員である（設計原則 P5）。"""
    assert "決めるのは教員です" in (DRAFT_PROMPT.system or "")


def test_the_judgements_never_reach_a_remote_model() -> None:
    """判定は学習者の提出についての記述なので個人データ（設計原則 P7）。"""
    provider = ScriptedProvider([json.dumps({"text": "x"})], local=False)
    writer = JustificationWriter(LlmGateway(provider), model="test")
    with pytest.raises(PolicyViolation):
        writer.draft(JUDGEMENTS)


def test_the_draft_is_returned_not_saved() -> None:
    """返るのは候補で、保存の判断はここではしない。"""
    writer, _ = _writer("テストケース 3 は正しく通っています。")
    assert writer.draft(JUDGEMENTS) == "テストケース 3 は正しく通っています。"
