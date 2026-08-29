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

    @property
    def solved(self) -> bool:
        return self.outcome is SolvabilityOutcome.SOLVED

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


__all__ = [
    "SolvabilityOutcome",
    "SolvabilityReport",
    "SolverAttempt",
]
