"""書き直しに渡す「教員からの指示」を固定する（#306）。

固定したいのは 3 つ。

届く         指示はそのままプロンプトに入る。必須事項も希望も同じ列に入る。
節ごと消える 指示が無いときは節そのものを出さない（「指示は空」と読ませない）。
版が動く     文面を変えたら版が上がる（P8）。

モデルは台本つきプロバイダで、ネットワークには出ない。
"""

from __future__ import annotations

import json

from aijudge_admin.revision import PROMPT, TaskReviser, _instructions_section
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

CRITERIA = (("正しさ", "すべてのテストケースを通ること"),)
VOCABULARY = (("cs.loops.termination", "繰り返しの停止"),)

REVISED = json.dumps(
    {
        "statement": "2 つの整数（各 0 以上 100 以下）の和を出力しなさい。",
        "changes": ["入力の範囲を明記した"],
        "knowledge_components": ["cs.loops.termination"],
    },
    ensure_ascii=False,
)


def _reviser() -> tuple[TaskReviser, ScriptedProvider]:
    provider = ScriptedProvider([REVISED])
    return TaskReviser(LlmGateway(provider), model="stub"), provider


def _sent(provider: ScriptedProvider) -> str:
    return "\n".join(m.content for m in provider.calls[0].messages)


def test_the_instructions_reach_the_prompt() -> None:
    """**何を直してほしいかは、読んだ教員がいちばんよく知っている。**

    観点との食い違いは機械的に見付かるが、「毎年ここで質問が来る」は教員しか
    知らない ── 指示を渡せないと、そこは何度書き直させても直らない。
    """
    reviser, provider = _reviser()
    reviser.revise(
        "2 つの整数の和を出力しなさい。",
        criteria=CRITERIA,
        vocabulary=VOCABULARY,
        instructions=("必ず入力の上限を明記すること", "できれば実行例を 2 つに増やしてほしい"),
    )

    sent = _sent(provider)
    assert "必ず入力の上限を明記すること" in sent
    assert "できれば実行例を 2 つに増やしてほしい" in sent, "希望として書いた指示が落ちている"


def test_a_revision_without_instructions_still_works() -> None:
    """**指示は入口であって、前提ではない。**

    渡さなくても改訂は成立する ── 観点との食い違いは指示が無くても見付かる。
    """
    reviser, provider = _reviser()
    result = reviser.revise(
        "2 つの整数の和を出力しなさい。", criteria=CRITERIA, vocabulary=VOCABULARY
    )

    assert result.changes == ("入力の範囲を明記した",)
    assert "## 教員からの指示" not in _sent(provider), "空の節を渡している"


def test_an_empty_instruction_list_gets_no_section() -> None:
    """**空の節を渡さない。** モデルは「指示が無い」ではなく「指示は空」と読む
    余地がある。書かれていない条件は、書かないことで伝える
    （`aijudge_admin.drafting._course_section` と同じ作法）。
    """
    assert _instructions_section(()) == ""
    assert _instructions_section(("", "   ")) == "", "空白だけの行で節が出ている"
    assert "必ず" in _instructions_section(("必ず標準入力から読むこと",))


def test_the_prompt_version_moved_with_the_wording() -> None:
    """文面を変えたら必ず版を上げる（P8）。版が同じで文面が違うと、過去に
    書き直した課題が何から出たのか追えなくなる。
    """
    assert PROMPT.id == "task_revision_ja@2"


def test_the_revision_prompt_carries_no_personal_data() -> None:
    """課題文も観点も教員が書いたもので、学習者のデータを含まない（P7）。

    区分が NON_PERSONAL である限り、ローカルでないプロバイダでも通る。
    ここが PERSONAL に変われば `PolicyViolation` で止まるので、**気づかず外に
    出ることはない**が、指示欄という自由入力が増えた以上、区分そのものを
    固定しておく。
    """
    provider = ScriptedProvider([REVISED], name="cloudish", local=False)
    reviser = TaskReviser(LlmGateway(provider), model="stub")

    result = reviser.revise(
        "2 つの整数の和を出力しなさい。",
        criteria=CRITERIA,
        vocabulary=VOCABULARY,
        instructions=("必ず入力の上限を明記すること",),
    )

    assert result.changes == ("入力の範囲を明記した",)
    sent = "\n".join(m.content for m in provider.calls[0].messages)
    assert "usr_" not in sent, "利用者 ID がプロンプトに入っている"
