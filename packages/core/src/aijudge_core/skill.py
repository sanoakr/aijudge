"""スキル状態とポートフォリオ用の証明。

習熟度の推定手法は差し替え可能にしておく（BKT から始め、DKT/AKT は後段）。
コアが持つのは「誰の・どの KC が・どれだけ習熟していて・根拠は何か」だけで、
推定アルゴリズムは S7 の中に閉じる。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .ids import CredentialId, CriterionScoreId, GradingRunId, KcId, TenantId, UserId


class MasteryModel(StrEnum):
    BKT = "bkt"
    DKT = "dkt"


class SkillEvidence(BaseModel):
    """習熟度の根拠となる採点結果への参照。

    ポートフォリオは「習熟度 0.82」ではなく
    「習熟度 0.82、根拠となる提出 5 件（うち教員確認済み 2 件）」を出す。
    そのために教員が確認済みかどうかをここで持つ。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grading_run_id: GradingRunId
    criterion_score_id: CriterionScoreId
    score_ratio: float = Field(ge=0.0, le=1.0)
    #: この判定をどれだけ確からしいと言えたか（`KcOutcome.confidence`・#335）。
    #:
    #: **`None` は 0 ではない。** この欄より前に積まれた根拠は値を持たない
    #: ── 0 として数えると、古い記録ほど自信が無かったことになる。画面は
    #: 「記録が無い」として扱う（根拠を示せない数字は出さない）。
    #:
    #: 採点が持っていた値をそのまま運ぶ。**ここで作らない** ── 確信度は
    #: 評価器が自己一貫性から測るもので、推定の側で足せる情報ではない。
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    human_verified: bool = False
    observed_at: datetime


class SkillPoint(BaseModel):
    """習熟度が動いた瞬間の記録（1 点）。**追記のみ。**

    `SkillState` は最新の 1 行を持ち替える推定値なので、**そこからは推移が
    読めない**。学期の途中で「この KC は上がっているのか」を言うには、動いた
    ときの値を残しておくしかない ── 根拠（`SkillEvidence`）は最新の 20 件に
    切られており、しかも BKT は観測列を畳むので、後から遡って再現できない。

    採点結果（P8）と同じ扱いで書き換えない。推定値そのものは後で手法を
    差し替えうるが、**そのとき出した値が何だったか**は記録である。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    learner_id: UserId
    kc_id: KcId
    mastery: float = Field(ge=0.0, le=1.0)
    #: この時点までに畳んだ観測の数。**同じ日に何度も動く**ので、点を
    #: 間引くときの順序はこちらで決める（時刻だけだと同着が並ぶ）。
    observation_count: int = Field(default=0, ge=0)
    model: MasteryModel = MasteryModel.BKT
    recorded_at: datetime


class SkillState(BaseModel):
    """学習者 × KC の現在の習熟度。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    learner_id: UserId
    kc_id: KcId
    mastery: float = Field(ge=0.0, le=1.0)
    model: MasteryModel = MasteryModel.BKT
    observation_count: int = Field(default=0, ge=0)
    evidence: tuple[SkillEvidence, ...] = ()
    updated_at: datetime

    @property
    def verified_evidence_count(self) -> int:
        return sum(1 for item in self.evidence if item.human_verified)


class CredentialExport(StrEnum):
    INTERNAL = "internal"
    OPEN_BADGES_3 = "ob3"


class Credential(BaseModel):
    """発行済みの証明。外部発行は Exporter アダプタが担当する。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CredentialId
    tenant_id: TenantId
    learner_id: UserId
    title: str = Field(min_length=1)
    kc_ids: tuple[KcId, ...] = Field(min_length=1)
    export: CredentialExport = CredentialExport.INTERNAL
    evidence_refs: tuple[GradingRunId, ...] = ()
    issued_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None
