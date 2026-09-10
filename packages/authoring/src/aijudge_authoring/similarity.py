"""既存の課題との重複を見つける（S2、設計方針 §5）。

同じ問題を作り直しても、学習者には練習にならない。生成のたびに既存の課題と
突き合わせ、近すぎるものを教員に示す。

## 2 つの尺度を持つ理由

**埋め込みは言い換えを捉え、字面は写しを捉える。** どちらか一方では足りない。

- 埋め込み（コサイン類似度）── 「2 数の和」と「2 つの整数を足す」を近いと
  判定できる。**埋め込みモデルが要る。**
- 字面（文字 3-gram の Jaccard）── モデルが要らず、どこでも動く。
  言い換えは捉えられないが、**写しは確実に捉える。**

埋め込みが使えない環境では字面だけで動かす。**「検査した」と嘘をつかない**
ように、どちらで測ったかを結果に残す。

## pgvector について

設計方針は pgvector を挙げているが、**それは索引であって能力ではない。**
1 コースの課題は数十〜数百件で、その規模では全件とのコサインを取る方が
速く、SQLite でも動く。索引が要るのは課題が万を超えてからで、そのときは
保存側だけを差し替える（この モジュールは索引を知らない）。
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# これを超えたら教員に示す。
#
# **自動で捨てる閾値ではない。** 同じ単元の課題は当然似るので、
# 高い類似度が欠陥とは限らない（第 3 回と第 4 回の練習問題など）。
DEFAULT_SIMILARITY_THRESHOLD = 0.85

# 字面の比較に使う文字 n-gram の長さ。
#
# 日本語には空白区切りが無いので単語では切れない。3 文字なら
# 「2 つの整数」のような短い言い回しの一致を拾える。
_SHINGLE = 3


class SimilarityMethod(StrEnum):
    EMBEDDING = "embedding"
    LEXICAL = "lexical"


class SimilarTask(BaseModel):
    """既存の課題 1 件との近さ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_version_id: str = Field(min_length=1)
    title: str = ""
    score: float = Field(ge=0.0, le=1.0)
    method: SimilarityMethod


class DuplicateReport(BaseModel):
    """重複の検査結果。**判定ではなく材料である。**

    近い課題があること自体は欠陥ではない。同じ単元を扱えば似るのが当然で、
    捨てるかどうかは教員が決める（設計原則 P5）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: SimilarityMethod
    threshold: float = Field(default=DEFAULT_SIMILARITY_THRESHOLD, ge=0.0, le=1.0)
    # 近い順。上位いくつかだけを持つ。
    nearest: tuple[SimilarTask, ...] = ()
    # 比較した既存課題の数。**0 なら何も確かめていない。**
    compared: int = Field(default=0, ge=0)

    @property
    def too_close(self) -> tuple[SimilarTask, ...]:
        return tuple(item for item in self.nearest if item.score >= self.threshold)

    @property
    def checked(self) -> bool:
        return self.compared > 0

    def summary(self) -> str:
        if not self.checked:
            return "重複: 比較できる既存の課題がありません（検査していません）"
        how = "埋め込み" if self.method is SimilarityMethod.EMBEDDING else "字面のみ"
        close = self.too_close
        if not close:
            top = f"（最も近いもので {self.nearest[0].score:.2f}）" if self.nearest else ""
            return f"重複: 既存 {self.compared} 件と比べて近いものはありません{top}／{how}"
        lines = [
            f"**近い課題があります**（{how}、既存 {self.compared} 件と比較）:",
        ]
        for item in close:
            lines.append(f"  {item.score:.2f}  {item.title or item.task_version_id}")
        lines.append("  同じ単元なら似るのが当然です。**捨てるかどうかは教員が決めます。**")
        if self.method is SimilarityMethod.LEXICAL:
            lines.append("  字面だけで測っています ── **言い換えた重複は見つかりません。**")
        return "\n".join(lines)


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """コサイン類似度。長さが違えば比較しない（別のモデルの出力である）。"""
    if len(left) != len(right) or not left:
        raise ValueError("次元の違うベクトルは比較できません")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if norm == 0.0:
        return 0.0
    # 数値誤差で 1 を僅かに超えることがある。
    return max(0.0, min(1.0, dot / norm))


def _shingles(text: str) -> set[str]:
    squeezed = "".join(text.split())
    if len(squeezed) < _SHINGLE:
        return {squeezed} if squeezed else set()
    return {squeezed[i : i + _SHINGLE] for i in range(len(squeezed) - _SHINGLE + 1)}


def lexical(left: str, right: str) -> float:
    """文字 3-gram の Jaccard。**言い換えは捉えられない。**"""
    a, b = _shingles(left), _shingles(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def rank(
    candidates: dict[str, tuple[str, float]],
    *,
    method: SimilarityMethod,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    top: int = 5,
) -> DuplicateReport:
    """`{課題版 ID: (題名, 類似度)}` を近い順に畳む。"""
    ordered = sorted(
        (
            SimilarTask(task_version_id=vid, title=title, score=score, method=method)
            for vid, (title, score) in candidates.items()
        ),
        key=lambda item: item.score,
        reverse=True,
    )
    return DuplicateReport(
        method=method,
        threshold=threshold,
        nearest=tuple(ordered[:top]),
        compared=len(candidates),
    )


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DuplicateReport",
    "SimilarTask",
    "SimilarityMethod",
    "cosine",
    "lexical",
    "rank",
]
