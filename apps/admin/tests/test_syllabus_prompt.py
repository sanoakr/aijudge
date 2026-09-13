"""シラバス・課題文から知識要素の候補を出すプロンプトの規則を固定する。

方針（2026-09-13）: **知識要素は登録済みの語彙から選ぶだけで、新しいキーは
作らない。** 語彙は骨格（`kc seed`）で決まり、画面から増やす経路は無い。
増やせると同じ概念が別のキーで二重に登録され、Q-matrix が割れる。

それ以前の版（@1〜@5）は「まだ無いものを挙げよ」の形で、既存が増えるほど
候補が 0 件になる圧力と、モデルが存在しない単位や日本語のキーを作る問題を
文言と関門で抑えていた。@6 で問いを「一覧から選べ」に変え、関門は
「登録済みか」の 1 つになった。

だからここで固定するのは、**一覧から選ばせていること**、**一覧に無いものは
理由を添えて落とすこと**、**一覧に無い候補を通していないこと**である。
"""

from __future__ import annotations

import json

from aijudge_admin.syllabus import PROMPT, SyllabusReader
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

EXISTING = ("cs.sdf.fundamentals.formatted_io", "cs.sdf.fundamentals.loops")

_PAYLOAD = {
    "course": {},
    "knowledge_components": [
        {"key": "cs.sdf.fundamentals.formatted_io", "label": "書式付き入出力"},
    ],
}


def _propose(payload: dict, existing=EXISTING):
    provider = ScriptedProvider([json.dumps(payload)])
    reader = SyllabusReader(LlmGateway(provider), model="test")
    return reader.propose("本文", namespaces=("cs",), existing_keys=existing), provider


def test_the_prompt_asks_to_choose_from_the_list_and_never_to_invent() -> None:
    rendered = PROMPT.template
    assert "この中から選びます" in rendered
    assert "一覧に無いキーを作らないでください" in rendered
    # 「まだ無いものを挙げよ」の形に戻っていないか（0 件への圧力そのもの）。
    assert "この一覧に無いもの" not in rendered
    assert "新しい知識要素のキー" not in rendered


def test_the_version_moved_with_the_wording() -> None:
    """文面を変えたら版を上げる（P8）。候補の出所を後から辿るため。"""
    assert PROMPT.version == "6"


def test_the_existing_keys_reach_the_prompt() -> None:
    _result, provider = _propose(_PAYLOAD)
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    for key in EXISTING:
        assert key in sent


def test_no_existing_keys_says_so_rather_than_leaving_it_blank() -> None:
    """空欄を渡すと、モデルには「既存が無い」のか「欄が壊れている」のか
    区別が付かない。"""
    _result, provider = _propose(_PAYLOAD, existing=())
    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "（まだありません）" in sent


def test_a_key_not_in_the_vocabulary_is_dropped_with_its_reason() -> None:
    """**登録済み以外は通さない。** 言い換えた新しいキーも、正しい形の
    未登録キーも、日本語のキーも同じ扱い ── 理由だけが違う。"""
    payload = {
        "course": {},
        "knowledge_components": [
            {"key": "cs.sdf.fundamentals.formatted_io", "label": "書式付き入出力"},
            {"key": "cs.sdf.fundamentals.pointers", "label": "ポインタ（未登録）"},
            {"key": "cs.sdf.基礎.配列の走査", "label": "配列の走査"},
        ],
    }
    result, _ = _propose(payload)
    assert [k.key for k in result.proposal.knowledge_components] == [
        "cs.sdf.fundamentals.formatted_io"
    ]
    reasons = {d.key: d.reason for d in result.discarded}
    assert "登録済みの知識要素にありません" in reasons["cs.sdf.fundamentals.pointers"]
    assert "キーの形が正しくありません" in reasons["cs.sdf.基礎.配列の走査"]


def test_what_was_dropped_is_reported_rather_than_silently_removed() -> None:
    """20 件出したはずが 14 件しか並んでいないとき、何が起きたのか画面から
    分からないのは、間違った候補が並ぶのと同じくらい悪い。"""
    payload = {
        "course": {},
        "knowledge_components": [{"key": "cs.sdf.fundamentals.nothing", "label": "無い"}],
    }
    result, _ = _propose(payload)
    assert result.proposal.knowledge_components == ()
    assert len(result.discarded) == 1


def test_a_clean_proposal_is_passed_through_unchanged() -> None:
    result, _ = _propose(_PAYLOAD)
    assert [k.key for k in result.proposal.knowledge_components] == [
        "cs.sdf.fundamentals.formatted_io"
    ]
    assert result.discarded == ()


def test_unit_keys_are_still_accepted_for_compatibility() -> None:
    """古い呼び出し（`unit_keys=`）が壊れないこと。使われはしない。"""
    provider = ScriptedProvider([json.dumps(_PAYLOAD)])
    reader = SyllabusReader(LlmGateway(provider), model="test")
    result = reader.propose(
        "本文", namespaces=("cs",), existing_keys=EXISTING, unit_keys=("cs.sdf.fundamentals",)
    )
    assert result.discarded == ()
