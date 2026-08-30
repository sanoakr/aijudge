"""テナント境界。

マルチテナント化の実装は PoC-5 だが、`tenant_id` を後から全テーブルに足すのは
現実的でないため、境界の語彙だけは最初から入れておく。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .grading import LatePenaltyStep
from .ids import CourseId, TenantId, UserId


class Role(StrEnum):
    LEARNER = "learner"
    INSTRUCTOR = "instructor"
    ASSISTANT = "assistant"
    ADMIN = "admin"


class Tenant(BaseModel):
    """機関。単独運用時は 1 件だけ存在する。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TenantId
    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Course(BaseModel):
    """開講科目。

    `auto_finalize_after_minutes` は成績の自動確定までの猶予（分）。
    None なら自動確定しない。

    **これは科目プロファイル（`subjects/*.yaml`）に置かない。** あちらは
    評価器の指名とタイムアウトを持つ採点の設定で、ブラウザから編集させない
    ものと決めてある（ADR 0002）。猶予は締切（`Task.due_at`）と同じ性質の
    運用値 ── 教員が学期中に決め、成績に直接効く ── なので、置き場所も
    権限も締切に揃える。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CourseId
    tenant_id: TenantId
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    term: str = Field(min_length=1)
    subject_profile: str = Field(min_length=1)
    # コースの概要・到達目標（Markdown）。シラバスから写して置く。
    #
    # **科目プロファイルには置かない。** あちらは採点の仕方の宣言で
    # （ADR 0002）、コードと同じレビューを通す前提の設定である。学期ごとに
    # 変わる事務データのためにブラウザから書ける口を開けると、1 人の操作で
    # 全員の採点が止まる経路ができる。
    description: str | None = None
    # このコースの共通ルーブリック（観点の宣言）。空なら組み込みの既定
    # （正しさ＋読みやすさ）。**課題が自分の観点を宣言していればそちらが勝つ。**
    #
    # コースに置くのは、観点の並びが科目の性質そのものだから ── レポートの
    # コースなら構成・実験設計・考察が全課題に共通で、課題ごとに書き写す
    # のは写し間違いを増やすだけである。個別に変えたい課題だけが宣言する。
    #
    # 形は `aijudge_authoring.CriterionSpec` の並び。core は器だけを持つ
    # （模型そのものを持つと、core が作問の語彙に依存する）。
    rubric: tuple[dict[str, object], ...] = ()
    # このコースだけの採点設定の上書き。空なら雛形（`subjects/*.yaml`）のまま。
    #
    # **コース単位にする。** 同じ雛形を使う他のコースには効かない ── だから
    # 教員が画面から触ってよい。プロファイルそのものを書き換えられるように
    # すると、1 人の操作で全員の採点が止まる（ADR 0002 が避けたのはそれで、
    # 「採点エンジンは科目を知らない」という性質は上書きしても変わらない）。
    #
    # 中身の形は `aijudge_grading.overrides` が決める。core は器だけを持つ。
    grading_overrides: dict[str, object] = Field(default_factory=dict)
    # 締切から何分で自動確定するか。None なら自動確定しない。
    # **分で持つ。** 「締切の 10 分後」を設定できないと、演習中に出して
    # その場で返す使い方ができない。時間単位では表せない粒度である。
    auto_finalize_after_minutes: int | None = Field(default=None, gt=0)
    # この科目で既定とする提出ファイル形式（拡張子）。空なら組み込みの既定。
    # **課題の指定が上書きする**（`aijudge_core.uploads.allowed_suffixes`）。
    upload_suffixes: tuple[str, ...] = ()
    # 遅延の減点の段。空なら遅延を見ない（＝減点しない）。
    #
    # **評価器には入れない。** 評価は遅延と独立に行い、これは評価の結果に
    # 対する減点である。置き場所を `auto_finalize_after_minutes` に揃えるのは
    # 同じ性質だから ── 成績に直接効き、教員が学期中に決める運用値。
    late_penalty_steps: tuple[LatePenaltyStep, ...] = ()

    @model_validator(mode="after")
    def _check_penalty_steps(self) -> Self:
        hours = [step.after_hours for step in self.late_penalty_steps]
        if hours != sorted(set(hours)):
            raise ValueError("late_penalty_steps must be sorted by after_hours and unique")
        return self


class Enrollment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    course_id: CourseId
    user_id: UserId
    role: Role

    @property
    def can_grade(self) -> bool:
        return self.role in (Role.INSTRUCTOR, Role.ASSISTANT, Role.ADMIN)
