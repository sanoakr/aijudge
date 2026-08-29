"""難度の推定（S2、設計方針 §5）。

新しい課題にはまだ提出が無いので、**似た既存課題の正答率から当てる。**
類似度は `similarity.py` が出したものをそのまま使う。

## これが測っているもの

**正答率であって、難しさそのものではない。** 正答率は課題の難しさ以外にも
いくつもの理由で動く ── いつ出したか（締切前か試験前か）、任意提出か必須か、
その回の受講者が誰か。**「正答率 0.4」を「難度が高い」と読み替えない。**

## 当てにならない場合をはっきり出す

3 件の提出から出した正答率は正答率ではない。似た課題が 1 件も無ければ
そもそも当てられない。**どちらも `NOT_MEASURED` を返す** ── 数字を出さない
ことと、出した数字が当てにならないことは、教員にとって意味が違う（ADR 0005）。

そして**類似度の質が推定の上限を決める。** 字面だけで測った「似た課題」は
「言い回しが似た課題」でしかなく、難度が似ている保証は無い。どの尺度で
近傍を選んだかを結果に残す。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .similarity import SimilarityMethod, SimilarTask

# 正答率とみなすのに要る提出数。
#
# **1 課題あたり**である。3 件で 2 件通れば 67% だが、それは 67% の証拠ではない
# （`evals/gates.yaml` の `min_sample_size` と同じ考え方、ADR 0005）。
MIN_ATTEMPTS = 15

# 推定に要る近傍の数。1 件に賭けると、その 1 件の癖がそのまま予測になる。
MIN_NEIGHBOURS = 2

# 「通った」とみなす総合点比。合否境界（科目プロファイルの既定）に合わせる。
DEFAULT_PASS_THRESHOLD = 0.6


class TaskOutcomeStats(BaseModel):
    """既存の課題 1 件の実績。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_version_id: str = Field(min_length=1)
    attempts: int = Field(ge=0)
    passed: int = Field(ge=0)

    @property
    def pass_rate(self) -> float | None:
        """提出が少なければ返さない。**少ない標本を率にしない。**"""
        if self.attempts < MIN_ATTEMPTS:
            return None
        return self.passed / self.attempts


class DifficultyEstimate(BaseModel):
    """難度の見立て。**判定ではなく材料である。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 予測した正答率。当てられなければ None。
    predicted_pass_rate: float | None = None
    method: SimilarityMethod | None = None
    # 根拠にした既存課題（類似度つき）。**空なら何も当てていない。**
    basis: tuple[SimilarTask, ...] = ()
    # 実績のある近傍が足りなかった理由。
    reason: str = ""

    @property
    def verdict(self) -> str:
        return "NOT_MEASURED" if self.predicted_pass_rate is None else "ESTIMATED"

    @property
    def label(self) -> str:
        """教員に見せる言葉。**数字だけを出さない。**"""
        rate = self.predicted_pass_rate
        if rate is None:
            return "不明"
        if rate >= 0.85:
            return "易しめ"
        if rate >= 0.6:
            return "標準"
        return "難しめ"

    def summary(self) -> str:
        if self.predicted_pass_rate is None:
            return f"難度: 推定していません（{self.reason or '理由の記録なし'}）"
        how = "埋め込み" if self.method is SimilarityMethod.EMBEDDING else "字面のみ"
        lines = [
            f"難度: {self.label}（似た課題の正答率から {self.predicted_pass_rate:.0%} と見込む）",
            f"  根拠（{how}で選んだ近傍 {len(self.basis)} 件）:",
        ]
        for item in self.basis:
            lines.append(f"    類似度 {item.score:.2f}  {item.title or item.task_version_id}")
        lines.append(
            "  **正答率は難しさそのものではありません。** 出題時期・任意か必須か・"
            "受講者によっても動きます。"
        )
        if self.method is SimilarityMethod.LEXICAL:
            lines.append(
                "  近傍は字面だけで選んでいます ── **言い回しが似ているだけ**の"
                "課題かもしれません。"
            )
        return "\n".join(lines)


def estimate(
    neighbours: tuple[SimilarTask, ...],
    stats: dict[str, TaskOutcomeStats],
    *,
    method: SimilarityMethod,
    min_neighbours: int = MIN_NEIGHBOURS,
) -> DifficultyEstimate:
    """近傍の正答率を類似度で重み付けして平均する。

    **実績の足りない近傍は捨てる**（数に入れない）。残った数が足りなければ
    推定しない ── 1 件に賭けると、その課題の癖がそのまま予測になる。
    """
    usable = []
    for item in neighbours:
        record = stats.get(item.task_version_id)
        rate = None if record is None else record.pass_rate
        if rate is not None:
            usable.append((item, rate))

    if len(usable) < min_neighbours:
        return DifficultyEstimate(
            method=method,
            reason=(
                f"実績のある似た課題が {len(usable)} 件しかありません"
                f"（{min_neighbours} 件必要。1 課題あたり提出 {MIN_ATTEMPTS} 件以上）"
            ),
        )

    total_weight = sum(item.score for item, _ in usable)
    if total_weight <= 0.0:
        return DifficultyEstimate(
            method=method, reason="近傍の類似度がすべて 0 でした"
        )
    predicted = sum(item.score * rate for item, rate in usable) / total_weight
    return DifficultyEstimate(
        predicted_pass_rate=round(predicted, 4),
        method=method,
        basis=tuple(item for item, _ in usable),
    )


__all__ = [
    "DEFAULT_PASS_THRESHOLD",
    "MIN_ATTEMPTS",
    "MIN_NEIGHBOURS",
    "DifficultyEstimate",
    "TaskOutcomeStats",
    "estimate",
]
