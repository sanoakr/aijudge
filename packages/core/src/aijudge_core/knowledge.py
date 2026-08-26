"""知識要素（KnowledgeComponent）と Q-matrix。

設計原則 P6「Q-matrix を全サブシステムの結節点にする」の実体。
作問（S2）・採点（S5）・スキル推定（S7）は互いを直接知らず、この語彙だけで繋がる。

名前空間の方針:
  KC は `namespace` と `path` に分ける。namespace は科目ごとに独立させる
  （"math", "cs", "phys" …）。科目横断の統合は PoC-4 まで意図的に先送りする。
  最初から共通体系を設計しようとすると、そこで全体が止まるため。
  統合するときは KcAlias（別 namespace の KC を同一視する）を足せば済むよう、
  KC 自体は namespace を跨いだ親子関係を持たない設計にしてある。
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import KcId, TaskVersionId

_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class KnowledgeComponent(BaseModel):
    """ひとつの知識要素。「積分の置換」「再帰の停止条件」といった粒度。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: KcId
    namespace: str
    path: tuple[str, ...] = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str | None = None
    parent_id: KcId | None = None

    @model_validator(mode="after")
    def _check_naming(self) -> Self:
        if not _NAMESPACE_RE.match(self.namespace):
            raise ValueError(f"invalid KC namespace: {self.namespace!r}")
        for segment in self.path:
            if not _SEGMENT_RE.match(segment):
                raise ValueError(f"invalid KC path segment: {segment!r}")
        return self

    @property
    def key(self) -> str:
        """人が読む正準キー。例: `math.calculus.integration.substitution`"""
        return ".".join((self.namespace, *self.path))

    @property
    def depth(self) -> int:
        return len(self.path)

    def is_descendant_of(self, other: KnowledgeComponent) -> bool:
        """`other` の下位 KC かどうか（同一 namespace 内でのみ成立する）。"""
        if self.namespace != other.namespace:
            return False
        return len(self.path) > len(other.path) and self.path[: len(other.path)] == other.path


class QMatrixEntry(BaseModel):
    """Task と KC の対応付け（Q-matrix の 1 セル）。

    従来の Q-matrix は 0/1 の二値だが、部分点のあるルーブリック採点では
    「この課題はこの KC をどれだけ問うているか」の重みが必要になるため
    `weight` を持たせる。`required` は DINA 系モデル向けに二値の情報も残すもの。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_version_id: TaskVersionId
    kc_id: KcId
    weight: float = Field(default=1.0, gt=0.0, le=1.0)
    required: bool = True
