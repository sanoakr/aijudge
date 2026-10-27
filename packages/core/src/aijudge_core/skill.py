"""スキル状態とポートフォリオ用の証明。

習熟度の推定手法は差し替え可能にしておく（BKT から始め、DKT/AKT は後段）。
コアが持つのは「誰の・どの KC が・どれだけ習熟していて・根拠は何か」だけで、
推定アルゴリズムは S7 の中に閉じる。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .ids import (
    CredentialId,
    CriterionScoreId,
    GradingRunId,
    KcId,
    SubmissionId,
    TaskId,
    TenantId,
    UserId,
)


class MasteryModel(StrEnum):
    BKT = "bkt"
    DKT = "dkt"


class MasteryScope(StrEnum):
    """どの観測から畳んだ習熟度か（ADR 0032）。

    学生にも教員にも、**演習だけ・試験だけ・両方を合わせた値**の 3 通りを見せる。
    合わせた値は試験を重く見た推定で、演習だけの値との差は「演習ではできて
    いるが、試験ではできていない」を読む材料になる。
    """

    ALL = "all"
    PRACTICE = "practice"
    EXAM = "exam"


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
    #: 監督のある試験での観測か（ADR 0032）。ポートフォリオで試験と演習の根拠を分けて示す。
    exam: bool = False
    observed_at: datetime


class SkillObservation(BaseModel):
    """習熟度の観測 1 件。**学習者 × KC × 課題で 1 件**（ADR 0032）。

    以前は採点 1 回ごとに観測を足していたので、出し直しや再採点の回数が
    習熟度を動かした。いまは課題ごとに**最終提出の結果**だけを持ち、新しい
    提出や再採点が来たら置き換える。習熟度はこの並びを BKT で畳み直して出す。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    learner_id: UserId
    kc_id: KcId
    task_id: TaskId
    submission_id: SubmissionId
    grading_run_id: GradingRunId
    criterion_score_id: CriterionScoreId
    score_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    #: 監督のある試験での観測か（`Task.exam`）。BKT のパラメータを分ける。
    exam: bool = False
    #: 提出の時刻。**どれが最終提出かと、畳む順序をこれで決める** ── 採点の
    #: 終わった時刻で決めると、再送や遅れた再採点で古い提出が「最新」になる。
    submitted_at: datetime
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
    #: どの観測から畳んだ値か。**既定は両方を合わせた値**（この欄より前の点）。
    scope: MasteryScope = MasteryScope.ALL
    recorded_at: datetime
    #: 作り直し（`aijudge-admin skill rebuild`・ADR 0032）が書いた点なら、その作り直しの
    #: 時刻。`recorded_at` は再生した採点の時刻（過去）になる。**ふだんの点は None。**
    #: 古い点は消さずに残し、描くときにどちらを使うかを `effective_points` が決める。
    rebuilt_at: datetime | None = None


def effective_points(points: Iterable[SkillPoint]) -> tuple[SkillPoint, ...]:
    """推移として描く点を選ぶ（ADR 0032 §5）。**記録は消さない。描く点だけを選ぶ。**

    最新の作り直し（`rebuilt_at` の最大）があれば、その時刻より前は**作り直した点**で、
    その時刻以降は**ふだんの点**で描く。作り直しより前のふだんの点（古い数え方の値）と、
    それより古い作り直しの点は描かない ── 混ぜると、同じ日に 2 つの数え方の値が並ぶ。

    作り直しが無ければ、渡された点をそのまま返す。順序は保つ（古い順で渡す）。
    """
    items = tuple(points)
    stamps = [point.rebuilt_at for point in items if point.rebuilt_at is not None]
    if not stamps:
        return items
    latest = max(stamps)
    return tuple(
        point
        for point in items
        if point.rebuilt_at == latest or (point.rebuilt_at is None and point.recorded_at >= latest)
    )


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
    #: 演習だけ・試験だけで畳んだ値と観測の数（ADR 0032）。**観測が無ければ None** ──
    #: 0 として持つと「試験を受けていない」と「試験で全くできなかった」が区別できない。
    practice_mastery: float | None = Field(default=None, ge=0.0, le=1.0)
    practice_count: int = Field(default=0, ge=0)
    exam_mastery: float | None = Field(default=None, ge=0.0, le=1.0)
    exam_count: int = Field(default=0, ge=0)

    def mastery_in(self, scope: MasteryScope) -> float | None:
        """その範囲の習熟度。**その範囲の観測が無ければ None。**"""
        if scope is MasteryScope.PRACTICE:
            return self.practice_mastery
        if scope is MasteryScope.EXAM:
            return self.exam_mastery
        return self.mastery

    def count_in(self, scope: MasteryScope) -> int:
        """その範囲の観測の数。"""
        if scope is MasteryScope.PRACTICE:
            return self.practice_count
        if scope is MasteryScope.EXAM:
            return self.exam_count
        return self.observation_count

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
