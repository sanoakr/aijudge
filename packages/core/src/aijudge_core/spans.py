"""Artifact 内の位置指定（ArtifactSpan）と根拠（Evidence）。

設計原則 P4「AI 出力は必ず根拠付き構造化データ」の土台。
採点結果の UI は「解答のこの部分が根拠でこの点数」をハイライト表示する。
それを成立させるには、コード・LaTeX・画像という異なるモダリティの位置を
ひとつの型で表現できなければならない。ここを曖昧にすると後から UI が作れない。

4 種類に絞った理由:
  - char  : 正規化済みテキストへの文字オフセット。LaTeX / Markdown の既定。
  - line  : 行単位。コードは行が意味の単位なので char より安定して読める。
  - region: 正規化座標の矩形。画像・PDF・図表領域。
  - whole : Artifact 全体。「レポート全体の構成」のような観点で使う。

スパンは常に `Artifact.content_hash` に対して定義する（Evidence が保持する）。
再正規化で内容が変われば hash が変わり、スパンは黙って別の場所を指すのではなく
「無効」と判定できる。これは採点の再現性（P8）のために必須。
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import ArtifactId, CriterionId


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WholeSpan(_Frozen):
    """Artifact 全体を指す。"""

    kind: Literal["whole"] = "whole"


class CharSpan(_Frozen):
    """正規化済みテキストへの文字オフセット（Unicode コードポイント単位、end は排他）。"""

    kind: Literal["char"] = "char"
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end <= self.start:
            raise ValueError("CharSpan.end must be greater than start")
        return self


class LineSpan(_Frozen):
    """行範囲。行は 1 始まり、両端を含む。列は省略可（省略時は行全体）。"""

    kind: Literal["line"] = "line"
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_col: int | None = Field(default=None, ge=1)
    end_col: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("LineSpan.end_line must be >= start_line")
        if (
            self.start_line == self.end_line
            and self.start_col is not None
            and self.end_col is not None
            and self.end_col < self.start_col
        ):
            raise ValueError("LineSpan.end_col must be >= start_col on the same line")
        return self


class RegionSpan(_Frozen):
    """矩形領域。座標は Artifact の幅・高さで正規化した 0.0〜1.0。

    解像度に依存しないため、サムネイルでも原寸でも同じ指定が使える。
    """

    kind: Literal["region"] = "region"
    page: int = Field(default=0, ge=0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.x + self.width > 1.0 + 1e-9:
            raise ValueError("RegionSpan exceeds the right edge")
        if self.y + self.height > 1.0 + 1e-9:
            raise ValueError("RegionSpan exceeds the bottom edge")
        return self


ArtifactSpan = Annotated[
    WholeSpan | CharSpan | LineSpan | RegionSpan,
    Field(discriminator="kind"),
]


class Evidence(_Frozen):
    """採点の根拠。「解答のどこを見てそう判断したか」を必ず持たせる。

    `quote` は表示用の抜粋。スパンから復元できるが、Artifact が失われた後や
    監査ログ上でも根拠が読めるように冗長に保持する。
    """

    artifact_id: ArtifactId
    artifact_content_hash: str = Field(min_length=1)
    span: ArtifactSpan
    quote: str | None = None
    note: str | None = None

    def targets(self, artifact_id: ArtifactId, content_hash: str) -> bool:
        """この Evidence が指定 Artifact の現在の内容に対して有効か。"""
        return self.artifact_id == artifact_id and self.artifact_content_hash == content_hash


class CriterionEvidence(_Frozen):
    """ルーブリック観点ごとにまとめた根拠。AI 評価器の出力単位でもある。"""

    criterion_id: CriterionId
    evidence: tuple[Evidence, ...] = ()
    rationale: str = Field(min_length=1)
