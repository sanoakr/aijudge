"""テナント境界。

マルチテナント化の実装は PoC-5 だが、`tenant_id` を後から全テーブルに足すのは
現実的でないため、境界の語彙だけは最初から入れておく。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .grading import LatePenaltyStep
from .ids import CourseGroupId, CourseId, TenantId, UserId
from .task import Aggregation


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
    # 共通ルーブリックの畳み方（AND / OR）。既定は OR ＝これまでの挙動。
    # **課題が指定すればそちらが勝つ**（`effective_aggregation`）。
    rubric_aggregation: Aggregation = Aggregation.OR
    # このコースが使う知識要素の正準キー。**空なら何も使わない**（#289）。
    #
    # 知識要素はコースに紐づかず、名前空間で共有される（設計原則 P6 の狙いで、
    # 習熟度が学期をまたいで積み上がるのもこの性質による）。だが同じ名前空間を
    # 複数のコースが使うほど、関係のない候補が増える ── `cs` を C と Python の
    # 科目が共有していれば、C の作問フォームに `cs.python.*` が並ぶ。
    #
    # **見にくいだけではない。** 課題に誤った知識要素を付けられるということで、
    # Q-matrix が課題の中身と食い違うと、学習者に「問われていない知識要素」の
    # 習熟度が付く。BKT は系列を畳むので、あとで気づいても取り消せない。
    #
    # **これは共有の語彙からの削除ではなく、このコースが使う範囲の宣言である。**
    # 外しても知識要素自体は残り、他のコースの Q-matrix は壊れない。
    #
    # 以前は空を「名前空間の全部」と読んでいた（後方互換）。それだと作った
    # ばかりのコースに 987 件が登録されているように見え、教員が責任を持って
    # 選んだ語彙にならない。いまは教員が明示的に足したものだけが範囲で、
    # 移行（`c7d2e91f4a63`）が既存コースの「課題が使っているもの」を書き込んだ。
    knowledge_components: tuple[str, ...] = ()
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


# グループ名の上限。**列の幅と揃える**（`course_groups.name` は 64）。SQLite は
# `VARCHAR(n)` の n を守らないので、模型で止めないと PostgreSQL でだけ落ちる
# （`audit_events.target_id` で本番のログインが止まった前例）。
MAX_GROUP_NAME_LENGTH = 64


class CourseGroup(BaseModel):
    """コースの中の名簿。**課題の出題先を絞るためにある**（追試・再試験）。

    受講登録とは別の層である ── 受講は「このコースの一員か」で、グループは
    「そのうち誰にこの課題を出すか」。名簿に入れられるのはそのコースの学習者
    だけ（`aijudge_admin.groups` が確かめる）。

    名前はコース内で一意。API はグループを名前で指す（スクリプトが ID を
    引き直さずに済むように）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CourseGroupId
    tenant_id: TenantId
    course_id: CourseId
    name: str = Field(min_length=1, max_length=MAX_GROUP_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        # 前後の空白で別のグループになると、同じ名前が 2 つ並んで見える。
        stripped = value.strip()
        if not stripped:
            raise ValueError("グループ名が空です")
        return stripped


class Enrollment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    course_id: CourseId
    user_id: UserId
    role: Role

    @property
    def can_grade(self) -> bool:
        return self.role in (Role.INSTRUCTOR, Role.ASSISTANT, Role.ADMIN)
