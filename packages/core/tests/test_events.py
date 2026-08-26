"""イベント契約のテスト。

サブシステム間の唯一の結合点なので、シリアライズのラウンドトリップと
判別可能性（discriminator）を固定しておく。ここが壊れると全体が壊れる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from aijudge_core import (
    EVENT_TYPES,
    DomainEvent,
    GradingCompleted,
    KcOutcome,
    Routing,
    SubmissionCreated,
    new_id,
)
from aijudge_core.ids import (
    CriterionScoreId,
    EventId,
    GradingRunId,
    KcId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
adapter: TypeAdapter[DomainEvent] = TypeAdapter(DomainEvent)


def test_every_registered_type_is_reachable_from_the_union() -> None:
    """EVENT_TYPES と DomainEvent の登録漏れを防ぐ。"""
    members = get_args(get_args(DomainEvent)[0])
    union_types = {member.model_fields["type"].default for member in members}
    assert union_types == set(EVENT_TYPES)
    assert {member for member in members} == set(EVENT_TYPES.values())


def test_grading_completed_round_trips_with_kc_outcomes() -> None:
    event = GradingCompleted(
        event_id=EventId(new_id("evt")),
        tenant_id=TenantId(new_id("ten")),
        occurred_at=NOW,
        grading_run_id=GradingRunId(new_id("grn")),
        submission_id=SubmissionId(new_id("sub")),
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        score_ratio=0.82,
        confidence=0.71,
        routing=Routing.REVIEW_REQUIRED,
        kc_outcomes=(
            KcOutcome(
                kc_id=KcId(new_id("kc")),
                score_ratio=0.82,
                confidence=0.71,
                criterion_score_ids=(CriterionScoreId(new_id("cs")),),
            ),
        ),
    )
    payload = event.model_dump_json()
    restored = adapter.validate_json(payload)
    assert isinstance(restored, GradingCompleted)
    assert restored == event


def test_the_discriminator_selects_the_right_event() -> None:
    event = SubmissionCreated(
        event_id=EventId(new_id("evt")),
        tenant_id=TenantId(new_id("ten")),
        occurred_at=NOW,
        submission_id=SubmissionId(new_id("sub")),
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        attempt=1,
        subject_profile="cs_intro",
    )
    restored = adapter.validate_python(event.model_dump())
    assert isinstance(restored, SubmissionCreated)


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "not.a.real.event"})
