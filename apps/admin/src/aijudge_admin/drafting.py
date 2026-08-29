"""Blueprint から課題の下書きを作らせる（S2、設計方針 §5）。

**app 層に置くのは、S2 と S6 が互いを import しないからである**
（`.importlinter` の独立契約）。語彙とプロンプトの入力は
`aijudge_authoring.drafting`、モデルを呼ぶのはここ。分けてあるので、
生成物の受け取り方はモデル無しで試験できる。

**作問は個人データを含まない**（`DataClass.NON_PERSONAL`）。学習者の解答も
氏名も渡さないので、ここはクラウドのモデルに出してよい唯一の経路である
（設計原則 P7）。弱い KC を狙う場合でも渡すのは KC のキーだけで、誰が
弱いかは渡さない。

生成しただけでは採点に使わない。**必ず門 2 つを通す**（`task_verifier.py`、
ADR 0008）。ここが返すのは候補であって課題ではない。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_authoring.drafting import Blueprint, TaskDraft, draft_to_spec
from aijudge_authoring.spec import TaskSpec
from aijudge_llm_gateway import (
    DataClass,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)

PROMPT = PromptTemplate(
    name="task_draft_ja",
    # 文面を変えたら必ず版を上げる（P8）。版が同じで文面が違うと、
    # 過去に生成した課題が何から出たのか追えなくなる。
    version="1",
    system=(
        "あなたは大学の理工系科目の課題を作る教員です。"
        "**問題文・参照解答・テストケースを必ず同時に作ります。**"
        "参照解答はすべてのテストケースを通らなければなりません。"
        "テストケースは入力ごとに出力が変わるものにします ── "
        "どの入力でも同じ出力になる組は、解答の中身を確かめられません。"
    ),
    template=(
        "次の条件で課題を 1 つ作ってください。\n\n"
        "## 問う知識要素\n{knowledge_components}\n\n"
        "## 難度\n{difficulty}\n\n"
        "## 言語\n{language}\n\n"
        "## 制約\n{constraints}\n\n"
        "## 既存の課題（似せないこと）\n{avoid}\n\n"
        "## テストケース数\n{test_case_count} 件。"
        "うち少なくとも 1 件は境界値（最小の入力、値が等しい場合など）にすること。\n"
    ),
)


@dataclass(frozen=True)
class DraftResult:
    """下書き 1 件と、それがどう作られたか。

    `prompt_id` と `model` を持つのは再現性のため（P8）。どの版のプロンプトと
    どのモデルが出したものかを `Provenance` に残せるようにしてある。
    """

    spec: TaskSpec
    draft: TaskDraft
    prompt_id: str
    model: str


class TaskDrafter:
    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def draft(self, blueprint: Blueprint, *, key: str) -> DraftResult:
        result = self._gateway.complete_structured(
            PROMPT,
            TaskDraft,
            model=self._model,
            # **個人データを含まない。** 渡すのは KC のキーと制約だけ。
            data_class=DataClass.NON_PERSONAL,
            max_tokens=self._max_tokens,
            knowledge_components="\n".join(f"- {kc}" for kc in blueprint.knowledge_components),
            difficulty=blueprint.difficulty.value,
            language=blueprint.language,
            constraints="\n".join(f"- {c}" for c in blueprint.constraints) or "（なし）",
            avoid="\n\n".join(blueprint.avoid_similar_to) or "（なし）",
            test_case_count=blueprint.test_case_count,
        )
        return DraftResult(
            spec=draft_to_spec(result.value, blueprint, key=key),
            draft=result.value,
            prompt_id=PROMPT.id,
            model=self._model,
        )
