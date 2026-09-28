"""教員だけが見る材料（評価基準・参照回答例）の渡し方（プロンプト版 4）。

観点の `description` は学習者の結果画面に出る。回答例をそこに書くと、一度
提出すれば答えが見える。AI にだけ渡す欄を足したが、**書かれていない課題の
プロンプトは版 3 と変わらない**ことが、既存の採点を動かさない条件である。
"""

from __future__ import annotations

from test_source_selection import _request, _verdict_json

from aijudge_core import ArtifactKind
from aijudge_eval_rubric_ai_judge import PROMPT, RubricAiJudge
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

SUBMISSION = "# socket を作る\nimport socket\n".encode()


def _prompt(request) -> str:
    provider = ScriptedProvider([_verdict_json(3)])
    RubricAiJudge(LlmGateway(provider), model="stub", samples=1).evaluate(request)
    return provider.calls[0].messages[1].content


def _with(request, *, notes: str | None = None, answer: str | None = None):
    criterion = request.criterion.model_copy(update={"judging_notes": notes})
    version = request.task_version.model_copy(
        update={"criteria": (criterion,), "reference_answer": answer}
    )
    return request.model_copy(update={"task_version": version, "criterion": criterion})


def test_the_prompt_version_is_4() -> None:
    assert PROMPT.version == "4"


def test_without_the_new_fields_nothing_is_added() -> None:
    """書かれていなければ、評価基準も参照回答例の節も出さない（版 3 と同じ文面）。"""
    user = _prompt(_request(ArtifactKind.CODE, SUBMISSION))
    assert "評価基準" not in user
    assert "参照回答例" not in user
    # 観点の説明の直後に、段階の見出しがそのまま続く
    assert "言えないことを区別しているか。\n\n## 段階\n" in user


def test_judging_notes_follow_the_description() -> None:
    notes = "各行の直前に、ネットワーク処理としての意味を書いているかを見る。"
    user = _prompt(_with(_request(ArtifactKind.CODE, SUBMISSION), notes=notes))
    head, rest = user.split("## 評価基準（採点者向け。学習者には見せていない）\n", 1)
    assert "言えないことを区別しているか。" in head
    assert rest.startswith(notes)
    assert rest.index(notes) < rest.index("## 段階")


def test_a_reference_answer_is_given_as_one_example() -> None:
    answer = "# サーバへ TCP で接続する\ns.connect((HOST, PORT))"
    user = _prompt(_with(_request(ArtifactKind.CODE, SUBMISSION), answer=answer))
    section = user.split("# 参照回答例（採点者が用意した一例。学習者には見せていない）\n", 1)[1]
    assert "一致しているかではなく" in section
    assert f"<<<参照回答例\n{answer}\n参照回答例>>>" in section
    # 提出物より前に置く（提出物の境界の外）
    assert user.index("参照回答例>>>") < user.index("# 学習者の提出物")


def test_blank_fields_count_as_absent() -> None:
    user = _prompt(_with(_request(ArtifactKind.CODE, SUBMISSION), notes="  \n", answer=""))
    assert "評価基準" not in user
    assert "参照回答例" not in user
