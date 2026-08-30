"""確定の根拠の下書きが、**理由そのものを作らないこと**を固定する。

根拠欄を必須にしているのは、教員が実際に下した判断を学習者に返すため
（ADR 0009 §4）。同じ文字列は一致度（κ）の標本に紐づく `HumanReview.comment`
でもある。差分から理由を作文させると、教員が考えていない理由が学習者に表示
され、その記録が測定に混ざる。ここで固定するのは 3 つ。

要点を渡す       材料は教員が書いたもので、AI の判定や差分ではない。
足さないと言う   プロンプトが「要点に無いことを足すな」と指示している。
学外へ出さない   要点は学習者の提出についての記述なので個人データ（P7）。
"""

from __future__ import annotations

import json

import pytest

from aijudge_admin.justification import PROMPT, JustificationWriter
from aijudge_llm_gateway import LlmGateway, PolicyViolation, ScriptedProvider


def _writer(text: str = "テストケース 3 は正しく通っています。"):
    provider = ScriptedProvider([json.dumps({"text": text})])
    return JustificationWriter(LlmGateway(provider), model="test"), provider


def test_the_points_the_instructor_wrote_are_what_is_sent() -> None:
    """**材料は教員が書いた要点。** AI の判定や段階の差分ではない。"""
    writer, provider = _writer()
    writer.polish("テスト3通ってる / AIは出力形式を誤判定")
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "テスト3通ってる" in sent
    assert "AIは出力形式を誤判定" in sent


def test_the_prompt_forbids_inventing_reasons() -> None:
    """整えるだけ、と指示していることを本文で確かめる。

    ここが緩むと、教員が考えていない理由が学習者に出る経路ができる。
    """
    assert "書かれていないことを足しません" in (PROMPT.system or "")


def test_the_adjusted_criteria_are_named_but_not_explained() -> None:
    """観点の題名だけは渡す。**名前の取り違えを防ぐためで、判断の中身ではない。**"""
    writer, provider = _writer()
    writer.polish("形式の差だけ", adjusted=("出力の正しさ",))
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "出力の正しさ" in sent


def test_the_points_never_reach_a_remote_model() -> None:
    """要点は学習者の提出についての記述なので個人データ（設計原則 P7）。"""
    provider = ScriptedProvider([json.dumps({"text": "文章"})], local=False)
    writer = JustificationWriter(LlmGateway(provider), model="test")
    with pytest.raises(PolicyViolation):
        writer.polish("テスト3通ってる")


def test_the_draft_is_returned_not_saved() -> None:
    """返るのは候補。**保存すると、誰も読んでいない文章が根拠として残る**（P5）。"""
    writer, _ = _writer("テストケース 3 は正しく通っています。")
    assert writer.polish("テスト3通ってる") == "テストケース 3 は正しく通っています。"
