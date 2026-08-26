"""ドメインイベント。サブシステム間の唯一の結合点（P2）。

    S3 --(SubmissionCreated)--> S5
    S5 --(GradingCompleted)---> S7, S9
    S7 --(SkillStateUpdated)--> S8, S2
    S2 --(TaskPublished)------> S7
    S8 --(CredentialIssued)---> S9

イベントは Outbox パターンで PostgreSQL にコミットし、リレーが
Redis Streams へ流す。購読側は必ず冪等に実装する（`event_id` で重複排除）。

ペイロードには集約全体ではなく「購読側が必要とする分だけ」を載せる。
全部を載せると、コアの変更が全サブシステムの再デプロイを強制するため。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .grading import KcOutcome, Routing
from .ids import (
    CredentialId,
    EventId,
    GradingRunId,
    KcId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)
from .knowledge import QMatrixEntry
from .skill import MasteryModel

SCHEMA_VERSION = 1


class _Event(BaseModel):
    """全イベント共通のエンベロープ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: EventId
    tenant_id: TenantId
    occurred_at: datetime
    schema_version: int = SCHEMA_VERSION


class SubmissionCreated(_Event):
    """S3 → S5。学習者が提出を確定させた（手書きなら書き起こし確定済み）。"""

    type: Literal["submission.created"] = "submission.created"
    submission_id: SubmissionId
    task_version_id: TaskVersionId
    learner_id: UserId
    attempt: int = Field(ge=1)
    subject_profile: str = Field(min_length=1)


class GradingCompleted(_Event):
    """S5 → S7, S9。採点が完了した。

    `kc_outcomes` を載せるのが要点。S7 は GradingRun の内部構造を知らずに
    習熟度を更新でき、S5 の採点実装を変えても S7 に波及しない（P6）。
    """

    type: Literal["grading.completed"] = "grading.completed"
    grading_run_id: GradingRunId
    submission_id: SubmissionId
    task_version_id: TaskVersionId
    learner_id: UserId
    score_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    routing: Routing
    kc_outcomes: tuple[KcOutcome, ...] = ()


class SkillStateUpdated(_Event):
    """S7 → S8, S2。習熟度が更新された。

    S2 はこれを購読して「クラスの弱い KC」を狙った作問ができるが、
    購読しなくても S2 は単独で動く（P2）。
    """

    type: Literal["skill.state_updated"] = "skill.state_updated"
    learner_id: UserId
    kc_id: KcId
    mastery: float = Field(ge=0.0, le=1.0)
    previous_mastery: float | None = Field(default=None, ge=0.0, le=1.0)
    model: MasteryModel
    observation_count: int = Field(ge=0)


class TaskPublished(_Event):
    """S2 → S7。教員レビューを通った TaskVersion が公開され、Q-matrix が増えた。"""

    type: Literal["task.published"] = "task.published"
    task_version_id: TaskVersionId
    subject_profile: str = Field(min_length=1)
    q_matrix: tuple[QMatrixEntry, ...] = ()
    ai_generated: bool = False


class CredentialIssued(_Event):
    """S8 → S9。証明が発行された。"""

    type: Literal["credential.issued"] = "credential.issued"
    credential_id: CredentialId
    learner_id: UserId
    kc_ids: tuple[KcId, ...] = Field(min_length=1)
    export: str = Field(min_length=1)


DomainEvent = Annotated[
    SubmissionCreated | GradingCompleted | SkillStateUpdated | TaskPublished | CredentialIssued,
    Field(discriminator="type"),
]

# トピック名 → イベント型。Outbox リレーと購読側のディスパッチに使う。
EVENT_TYPES: dict[str, type[_Event]] = {
    "submission.created": SubmissionCreated,
    "grading.completed": GradingCompleted,
    "skill.state_updated": SkillStateUpdated,
    "task.published": TaskPublished,
    "credential.issued": CredentialIssued,
}
