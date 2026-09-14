"""既にある課題を、いまの採点基準に合わせて書き直す（#306）。

## なぜ要るか

既存の課題は**入出力しか見ない前提**で書かれている。仕様が曖昧なまま、例と
本文が食い違ったまま、読みやすさのような観点への配慮が無いまま出題されている
── そこへコースの共通ルーブリックが「読みやすさ 0.3」と言っても、**問題文が
それを求めていない**ので学習者には減点の理由が分からない。

1 問ずつ手で直すには数が多い。**現在の基準に合わせた新しい版を作らせ、教員は
差分を読んで承認する**（ADR 0008 の承認待ちに乗せる・P5）。

## モデルにさせること・させないこと

**させるのは、読まなければ分からないことだけである。**

    問題文の書き直し   仕様の曖昧さ、例と本文の食い違い、観点の明示
    何を直したか       差分を読む教員の手掛かり
    知識要素の候補     問題文が実際に問うているもの（登録済みの語彙から）

**させないのは、決まっていることである。** 観点（ルーブリック）はコースの
共通ルーブリックが持っており、段階の記述まで含めて既に決まっている ── そこを
モデルに書かせると、コースごとに決めた段階が課題ごとに割れる。段階の設計は
教員の仕事であって、生成の対象ではない（P5）。

**入出力セットと参照解答も触らない。** 期待出力は参照解答を走らせて作るもので
（#305）、問題文の書き直しと混ぜると「どちらの都合で期待出力が変わったのか」が
辿れなくなる。改訂で入出力の形式を変えないよう、プロンプトでも釘を刺す。

**題名も変えない。** 同じ課題の改訂であって別の課題ではない ── 題名が変わると
一覧で別物に見え、学習者は「前に見た問題が消えた」と読む。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_authoring import RevisedTask
from aijudge_llm_gateway import (
    DataClass,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)

PROMPT = PromptTemplate(
    name="task_revision_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは大学の理工系科目の課題を、現在の採点基準に合わせて書き直す教員です。"
        "**同じ課題を書き直します。別の課題にはしません** ── 問うている内容と"
        "難易度は変えず、曖昧さ・食い違い・書き漏らしを直します。"
        "**採点の観点は与えられたものがそのまま使われます。** 観点が求めている"
        "ことが問題文から読み取れるように書きます ── 読みやすさを採点するのに"
        "問題文がそれを求めていなければ、学習者には減点の理由が分かりません。"
        "**入出力の形式は変えません** ── 変えると、いまあるテストケースがすべて"
        "外れます。"
        "JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template=(
        "## いまの問題文\n{statement}\n\n"
        "## この課題の採点の観点（変更しません）\n{criteria}\n\n"
        "## 選べる知識要素（この中からだけ選ぶ）\n{vocabulary}\n\n"
        "## いま付いている知識要素\n{current_kcs}\n\n"
        "## 直すこと\n"
        "- 仕様の曖昧さ（入力の範囲、出力の形式、端の場合の扱い）\n"
        "- 例と本文の食い違い\n"
        "- **観点が求めていることが問題文から読み取れない箇所**\n"
        "- 誤字と、読みにくい言い回し\n\n"
        "直すところが無ければ、問題文はそのまま返し changes を空にします。\n\n"
        '出力する JSON の形: {{"statement": "書き直した問題文全体", '
        '"changes": ["直した点を 1 行ずつ"], '
        '"knowledge_components": ["cs.loops.termination"]}}\n'
    ),
)


@dataclass(frozen=True)
class RevisionResult:
    """書き直した課題と、その出所（P8）。"""

    statement: str
    changes: tuple[str, ...]
    knowledge_components: tuple[str, ...]
    prompt_id: str
    model: str

    @property
    def unchanged(self) -> bool:
        """直すところが無かったか。

        **空の改訂を承認待ちに積まない。** 積むと、教員は差分の無い版を 1 件
        ずつ開いて確かめることになる ── 承認待ちの一覧は「人が見るべきもの」
        だけを載せる場所である。
        """
        return not self.changes


class TaskReviser:
    """課題文を現在の基準に合わせて書き直す。**保存はしない。**"""

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def revise(
        self,
        statement: str,
        *,
        criteria: tuple[tuple[str, str], ...],
        vocabulary: tuple[tuple[str, str], ...],
        current_kcs: tuple[str, ...] = (),
    ) -> RevisionResult:
        """`criteria` は (題名, 説明)、`vocabulary` は (正準キー, 説明)。"""
        result = self._gateway.complete_structured(
            PROMPT,
            RevisedTask,
            model=self._model,
            # 課題文も観点も教員が書いたもので、学習者のデータを含まない（P7）。
            data_class=DataClass.NON_PERSONAL,
            timeout_seconds=300.0,
            max_tokens=self._max_tokens,
            statement=statement[:8000],
            criteria="\n".join(f"- {title}: {description}" for title, description in criteria)
            or "（観点の宣言がありません）",
            vocabulary="\n".join(f"- {key}: {label}" for key, label in vocabulary)
            or "（このコースは知識要素を使っていません）",
            current_kcs="・".join(current_kcs) or "（付いていません）",
        )
        # **語彙に無い候補は落とす**（#299）。新しい知識要素は作れない ──
        # 体系は管理者が投入する骨格で決まる。
        known = {key for key, _ in vocabulary}
        return RevisionResult(
            statement=result.value.statement,
            changes=tuple(line.strip() for line in result.value.changes if line.strip()),
            knowledge_components=tuple(
                key for key in result.value.knowledge_components if key in known
            ),
            prompt_id=PROMPT.id,
            model=self._model,
        )


__all__ = ["PROMPT", "RevisionResult", "TaskReviser"]
