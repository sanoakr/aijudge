"""知識要素（KnowledgeComponent）と Q-matrix。

設計原則 P6「Q-matrix を全サブシステムの結節点にする」の実体。
作問（S2）・採点（S5）・スキル推定（S7）は互いを直接知らず、この語彙だけで繋がる。

名前空間の方針:
  KC は `namespace` と `path` に分ける。namespace は**分野**ごとに独立させる
  （"math", "cs", "phys" …）。コースでも科目プロファイルでもない ── 同じ
  `cs.loops.termination` を複数のコースが参照するのは例外ではなく既定で、
  ID がキーから決まる（`kc_id_for`）ので何もしなくても繋がる。習熟度が
  コースと学期をまたいで積み上がるのはこの性質による（設計原則 P6）。

  分野をまたいだ統合は PoC-4 まで意図的に先送りする。最初から共通体系を
  設計しようとすると、そこで全体が止まるため。統合するときは
  `superseded_by`（別の KC に寄せる）で済むよう、KC 自体は namespace を
  跨いだ親子関係を持たない設計にしてある。

体系が荒れないようにするための規則（`aijudge_admin.kc` が強制する）:

  1. namespace は科目プロファイルが宣言したものだけ。教員は増やせない
  2. 新しい KC は**既存 KC の子**としてのみ足せる（孤立キーを作らせない）
  3. **改名しない。** ID がキーから導かれ Q-matrix は追記のみ（P8）なので、
     誤りは `deprecated` にして `superseded_by` で後継を指す
  4. AI には KC を作らせない。生成は登録済みからの選択だけ

  禁止ではなく「追加を明示的な行為にする」ことを狙っている。禁止すると
  教員は既存の近いキーに無理やり寄せ、構造としてはより悪くなる。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import KcId, TaskVersionId, UserId, derived_id

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
    # 引退した KC。**消さない** ── ID はキーから導かれ、Q-matrix は追記のみ
    # なので、消すと過去の課題が何を問うていたのか辿れなくなる（P8）。
    deprecated: bool = False
    # 後継。誤って作った KC を正しいものに寄せるときに指す。
    superseded_by: KcId | None = None
    # 誰がいつ足したか。**共有される語彙なので出所を残す** ── 1 つのコースの
    # 教員が足した KC は、同じ namespace を使う他のコースにも入る。
    created_by: UserId | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _check_naming(self) -> Self:
        if not _NAMESPACE_RE.match(self.namespace):
            raise ValueError(f"invalid KC namespace: {self.namespace!r}")
        for segment in self.path:
            if not _SEGMENT_RE.match(segment):
                raise ValueError(f"invalid KC path segment: {segment!r}")
        if self.superseded_by is not None:
            if self.superseded_by == self.id:
                raise ValueError("KC は自分自身を後継にできません")
            if not self.deprecated:
                # 後継を指すのは引退したときだけ。現役の KC が別の KC を
                # 指していると、どちらを使えばよいのか誰にも分からない。
                raise ValueError("superseded_by を持つ KC は deprecated でなければなりません")
        return self

    @property
    def parent_key(self) -> str | None:
        """親の正準キー。第 1 階層なら None。"""
        if len(self.path) < 2:
            return None
        return ".".join((self.namespace, *self.path[:-1]))

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


def kc_id_for(key: str) -> KcId:
    """正準キーから KC の ID を導く。**ここが唯一の導出点。**

    課題（`q_matrix_for`）と体系（`aijudge_admin.kc`）が別々に導出すると、
    片方の綴りを直したときにもう片方が追随しない。
    """
    return KcId(derived_id("kc", key))


def parse_kc_key(key: str) -> tuple[str, tuple[str, ...]]:
    """正準キーを namespace と path に割る。形が不正なら ValueError。"""
    parts = key.split(".")
    if len(parts) < 2:
        raise ValueError(f"KC のキーは `namespace.path…` の形です: {key!r}")
    namespace, *path = parts
    if not _NAMESPACE_RE.match(namespace):
        raise ValueError(f"invalid KC namespace: {namespace!r}")
    for segment in path:
        if not _SEGMENT_RE.match(segment):
            raise ValueError(f"invalid KC path segment: {segment!r}")
    return namespace, tuple(path)


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
