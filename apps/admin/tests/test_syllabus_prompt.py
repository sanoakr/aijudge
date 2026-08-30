"""シラバスから知識要素の候補を出すプロンプトの規則を固定する。

**既にある知識要素の説明が要点。** ここを誤ると候補が 0 件になり、しかも
**例外ではなく空の結果として返る**ので、画面には「候補が出ませんでした」
としか出ない。二度同じ形で壊れた。

  @1  「重複を作らないこと」としか書いていない
      → 根が 1 つあるだけで 0 件（実測 2026-08-30）
  @2  「分野の根が既にあることは…ではありません」と根について言い足す
      → **子が 1 件でもあると 0 件**（実測 2026-08-31・#30）
  @3  件数にも形にも依らない言い方に直した
  @4  **問いそのものを変えた**（#41）

@2 が効かなかったのは、**症状に合わせて直したから**である。モデルが読んで
いるのは「既にあるものがある＝網羅されつつある」という関係で、根か子かでは
ない。@3 はその読みを打ち消す文言で凌いでいた。

@4 は打ち消す必要そのものを無くす。**「まだ無いものを挙げよ」と訊く限り、
既存が増えるほど「もう挙げるものが無い」への圧力が掛かる。**「このコースが
扱う知識要素を挙げよ」に変え、既にあるものは既存のキーをそのまま書かせる。
重複の禁止は「挙げるな」ではなく「同じ概念に新しいキーを作るな」になる。

だからここで固定するのは文面そのものではなく、**既存を挙げてよいと言って
いること**と、**特定の形・件数に限定していないこと**である。
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


def test_the_prompt_does_not_depend_on_how_many_already_exist() -> None:
    """**既存の件数に依らない言い方であること。**

    ここが「分野の根が既にあることは…」のように**特定の形に限定**されると、
    その形から外れた瞬間に 0 件へ戻る。実際にそうなった ── 根について言った
    だけの版（@2）では、**子が 1 件でもあると 0 件**になり、この機能は
    「一度使うと使えなくなる」形だった（#30）。

    症状ごとに直していると、既存が増えるたびに同じことが起きる。
    """
    rendered = PROMPT.template
    # 件数に依らないと明言しているか。
    assert "既にある数がいくつであっても" in rendered
    assert "網羅されたとは考えないでください" in rendered
    # 「根が」のような特定の形への限定に戻っていないか。
    assert "分野の根（例" not in rendered
    # **「この一覧に無いものを挙げよ」に戻っていないか。** それが 0 件への
    # 圧力そのもので、@1〜@3 はその圧力を文言で打ち消そうとしていた。
    assert "この一覧に無いもの" not in rendered


def test_the_existing_list_is_not_a_list_of_things_to_omit() -> None:
    """**既にあるものも挙げてよい**と言っているか（#41）。

    教員がこの画面で決めることは 2 つある ── 体系に足すものと、**このコースが
    使う範囲**（`Course.knowledge_components`）。既存を挙げさせないと、後者を
    決める材料が画面のどこにも出ない。#37 で範囲外の知識要素を一覧から隠した
    ので、候補にも一覧にも出ない ── 存在を知る手段が無くなる。
    """
    rendered = PROMPT.template
    assert "既にあるものも挙げてください" in rendered
    # 重複の禁止は「挙げるな」ではなく「新しいキーを作るな」。
    assert "一字も変えずにそのまま" in rendered
    assert "言い換えた新しいキーを作らないでください" in rendered


def test_the_empty_answer_is_reserved_for_actually_nothing_left() -> None:
    """空で返してよい条件を 1 つに絞る。**曖昧だと「もう十分」で空になる。**"""
    assert "1 つも扱っていないときだけ" in PROMPT.template


def test_the_version_moved_with_the_wording() -> None:
    """文面を変えたら版を上げる（P8）。版が同じで文面が違うと、過去に出した
    候補が何から出たのか追えなくなる。
    """
    assert PROMPT.id == "syllabus_to_candidates_ja@4"


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
