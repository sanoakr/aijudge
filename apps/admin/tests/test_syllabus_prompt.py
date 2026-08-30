"""シラバスから知識要素の候補を出すプロンプトの規則を固定する。

**既にある知識要素の説明が要点。** 「重複を作らない」としか書いていなかった
ため、`cs.c_language` が既にある状態で C 言語のシラバスを渡すと**候補が
0 件**になった（実測 2026-08-31）。モデルが「もう網羅されている」と読む。

根が既にあることは「そこにもう出す候補が無い」という意味ではない。むしろ
その下にぶら下げるのが普通の使い方で、プロンプトはそれを言う必要がある。
"""

from __future__ import annotations

import json

from aijudge_admin.syllabus import PROMPT, SyllabusReader
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

_PAYLOAD = {
    "course": {},
    "knowledge_components": [
        {"key": "cs.c_language.formatted_io", "label": "書式付き入出力"},
    ],
}


def test_the_existing_components_are_described_as_somewhere_to_hang_from() -> None:
    """**「重複を作らない」だけでは足りない。**

    ここが緩むと、根が登録されているコースで候補が 0 件になる。
    """
    rendered = PROMPT.template
    assert "同じものを作らないでください" in rendered
    # 既存が「ぶら下げる先」でもあることを言っているか。
    assert "ぶら下げる先" in rendered
    assert "もう出す候補が無いという意味ではありません" in rendered


def test_the_version_moved_with_the_wording() -> None:
    """文面を変えたら版を上げる（P8）。版が同じで文面が違うと、過去に出した
    候補が何から出たのか追えなくなる。
    """
    assert PROMPT.id == "syllabus_to_candidates_ja@2"


def test_the_existing_keys_reach_the_prompt() -> None:
    provider = ScriptedProvider([json.dumps(_PAYLOAD)])
    reader = SyllabusReader(LlmGateway(provider), model="test")
    reader.propose("シラバス本文", namespaces=("cs",), existing_keys=("cs.c_language",))

    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "cs.c_language" in sent
    assert "シラバス本文" in sent


def test_no_existing_keys_says_so_rather_than_leaving_it_blank() -> None:
    """空欄を渡すと、モデルには「既存が無い」のか「欄が壊れている」のか
    区別が付かない。
    """
    provider = ScriptedProvider([json.dumps(_PAYLOAD)])
    reader = SyllabusReader(LlmGateway(provider), model="test")
    reader.propose("シラバス本文", namespaces=("cs",), existing_keys=())

    sent = "\n".join(message.content for message in provider.calls[0].messages)
    assert "（まだありません）" in sent
