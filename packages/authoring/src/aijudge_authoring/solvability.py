"""解答可能性の検査（S2、設計方針 §5）。

**問題文だけを渡して別のモデルに解かせ、その解答がテストケースを通るかを見る。**
教員レビューの**前**に置き、承認・却下の判断材料にする。

## 参照解答と突き合わせない

正しいプログラムは何通りもある。生成された解答が参照解答と一字一句同じで
ないことは、何の情報でもない。見るのは**振る舞い**、すなわち門 1 と同じ
「テストケースを全部通るか」である。

## 落ちたことは却下の理由ではない

解けなかった原因は 2 つに分かれ、**機械には分けられない。**

- 課題文が曖昧、または課題そのものが解けない（＝作問の欠陥）
- 単に難しい（＝作問としては正しい）

だから自動で却下しない。教員に「別のモデルは解けませんでした」と示し、
判断は人が持つ（設計原則 P5）。**自動で捨てると、難しい良問から先に消える。**
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SolvabilityOutcome(StrEnum):
    # 別のモデルが解けた。課題文だけで解答に至れる証拠になる。
    SOLVED = "solved"
    # 解こうとしたが、テストケースを通らなかった。**却下の理由ではない。**
    UNSOLVED = "unsolved"
    # 検査していない（モデルが無い、参照解答が無い等）。**合格ではない。**
    NOT_RUN = "not_run"


class SolverAttempt(BaseModel):
    """解答役のモデルに返させる構造化出力（設計原則 P4）。"""

    model_config = ConfigDict(extra="ignore")

    # 何をする課題だと読んだか。**解答の前に書かせる。**
    # 食い違いが起きたとき、それが読解の失敗か実装の失敗かを分けられる。
    understanding: str = Field(min_length=1, max_length=1000)
    solution: str = Field(min_length=1, max_length=20000)
    # 宣言された KC のうち、解くのに実際に必要だったもの。
    #
    # **候補を示して選ばせる。** 体系の中から自由に挙げさせても、こちらの
    # 正準キーを知らないモデルは似て非なる名前を返す。選択にすれば、
    # 返ってくるのは宣言と突き合わせられる形になる。
    exercised: tuple[str, ...] = ()


class SolvabilityReport(BaseModel):
    """検査の結果。**教員が読む文書でもある。**

    通ったかどうかだけでなく、解答役が課題をどう読んだかを残す ── 課題文が
    曖昧なとき、その曖昧さは「読み違え」として現れる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SolvabilityOutcome
    # 解答役のモデル。**下書きを作ったモデルとは別**であるべき。
    solver_model: str = ""
    understanding: str = ""
    detail: str = ""
    # Blueprint が宣言した KC と、解答役が「実際に要った」と答えた KC。
    declared_kcs: tuple[str, ...] = ()
    exercised_kcs: tuple[str, ...] = ()

    @property
    def solved(self) -> bool:
        return self.outcome is SolvabilityOutcome.SOLVED

    @property
    def unexercised_kcs(self) -> tuple[str, ...]:
        """宣言されているのに、解くのに要らなかった KC。

        **欠陥とは限らない。** 解答役が別の書き方で解いた可能性があるし、
        そもそも解けていなければ何も言えない。Q-matrix は S7 の習熟度が
        全面的に依存する対応表なので、**食い違いは教員に見せる** ── 見せずに
        通すと、問われていない KC の習熟度が学習者に付く。
        """
        if not self.solved:
            return ()
        return tuple(kc for kc in self.declared_kcs if kc not in set(self.exercised_kcs))

    def summary(self) -> str:
        if self.outcome is SolvabilityOutcome.NOT_RUN:
            return f"解答可能性: 検査していません（{self.detail or '理由の記録なし'}）"
        if self.solved:
            return f"解答可能性: {self.solver_model} が課題文だけから解けました"
        lines = [
            f"解答可能性: {self.solver_model} は解けませんでした",
            "  **これは却下の理由ではありません。** 課題文が曖昧なのか、",
            "  単に難しいのかは機械には分かりません。",
        ]
        if self.understanding:
            lines.append(f"  解答役の読み: {self.understanding}")
        if self.detail:
            lines.append(f"  落ちた理由: {self.detail}")
        return "\n".join(lines)

    def kc_note(self) -> str | None:
        """KC の食い違いを教員に見せる文。無ければ None。"""
        missing = self.unexercised_kcs
        if not missing:
            return None
        return (
            "**宣言した知識要素のうち、解くのに要らなかったもの: "
            + "・".join(missing)
            + "**\n  解答役は別の書き方をしただけかもしれません。ただし Q-matrix は"
            "\n  習熟度の土台なので、問うていない KC が残ると、学習者に"
            "\n  身に付いていない力が付いたことになります。"
        )


__all__ = [
    "SolvabilityOutcome",
    "SolvabilityReport",
    "SolverAttempt",
]
