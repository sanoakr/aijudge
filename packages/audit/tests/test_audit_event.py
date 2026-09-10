"""監査記録の型が守ること。

要点は 3 つ。**帰属を偽らない**、**追記専用**、**学習者データの置き場に
しない**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aijudge_audit import (
    MAX_DETAIL_CHARS,
    ActorKind,
    AuditAction,
    AuditEvent,
    AuditRecorder,
    DuplicateAuditEvent,
    InMemoryAuditLog,
)
from aijudge_core.ids import TenantId, UserId

TENANT = TenantId("ten_1")
INSTRUCTOR = UserId("usr_teacher")


def _event(**overrides) -> AuditEvent:
    base = {
        "id": "aud_1",
        "at": datetime(2026, 9, 10, tzinfo=UTC),
        "tenant_id": TENANT,
        "actor_kind": ActorKind.USER,
        "actor_user_id": INSTRUCTOR,
        "actor_role": "instructor",
        "action": AuditAction.GRADE_FINALIZED,
        "target_type": "submission",
        "target_id": "sub_1",
        "summary": "成績を確定した",
    }
    return AuditEvent(**{**base, **overrides})


def test_a_user_action_must_name_the_user() -> None:
    with pytest.raises(ValidationError):
        _event(actor_kind=ActorKind.USER, actor_user_id=None)


def test_a_system_action_is_not_attributed_to_anyone() -> None:
    """締切経過による自動確定に操作者はいない。

    誰かに帰属させると、教員が確定していないものを確定したことにする。
    ADR 0010 が `Finalization` と `HumanReview` を分けて塞いだ区別が、
    監査の側で壊れる。
    """
    with pytest.raises(ValidationError):
        _event(actor_kind=ActorKind.SYSTEM, actor_user_id=INSTRUCTOR)

    event = _event(actor_kind=ActorKind.SYSTEM, actor_user_id=None, actor_role=None)
    assert event.actor_kind is ActorKind.SYSTEM


def test_the_record_cannot_be_changed_after_the_fact() -> None:
    """書き換えられる記録は証拠にならない（P8 と同じ理由）。"""
    event = _event()
    with pytest.raises(ValidationError):
        event.summary = "別の話"  # type: ignore[misc]


def test_detail_is_not_a_place_for_submissions() -> None:
    """監査ログは消さない場所にある。そこへ本文を写せば、消えない場所に増える。"""
    with pytest.raises(ValidationError):
        _event(detail={"source": "x" * (MAX_DETAIL_CHARS + 100)})

    # 差分の前後の値ならこの上限に収まる。
    event = _event(detail={"grace_minutes": {"before": 0, "after": 30}})
    assert event.detail["grace_minutes"]["after"] == 30


def test_the_role_is_burned_in_at_the_time_of_the_action() -> None:
    """あとで役割が変わっても、そのときの権限で読めること。"""
    assert _event(actor_role="ta").actor_role == "ta"


def test_the_log_refuses_to_write_the_same_row_twice() -> None:
    log = InMemoryAuditLog()
    log.record(_event())
    with pytest.raises(DuplicateAuditEvent):
        log.record(_event())


def test_what_happened_to_this_submission() -> None:
    log = InMemoryAuditLog()
    at = datetime(2026, 9, 10, tzinfo=UTC)
    log.record(_event(id="aud_1", at=at, action=AuditAction.REVIEW_RECORDED))
    log.record(_event(id="aud_2", at=at + timedelta(hours=1)))
    log.record(_event(id="aud_3", target_id="sub_other"))

    rows = log.list_for_target("submission", "sub_1")
    assert [row.id for row in rows] == ["aud_2", "aud_1"]  # 新しい順


def test_recent_rows_can_be_narrowed_by_action_and_time() -> None:
    log = InMemoryAuditLog()
    at = datetime(2026, 9, 10, tzinfo=UTC)
    log.record(_event(id="aud_1", at=at, action=AuditAction.LOGIN_FAILED))
    log.record(_event(id="aud_2", at=at + timedelta(days=1)))
    log.record(_event(id="aud_3", at=at, tenant_id=TenantId("ten_other")))

    assert [r.id for r in log.list_recent(TENANT)] == ["aud_2", "aud_1"]
    assert [r.id for r in log.list_recent(TENANT, action=AuditAction.LOGIN_FAILED)] == ["aud_1"]
    assert [r.id for r in log.list_recent(TENANT, since=at + timedelta(hours=1))] == ["aud_2"]


def test_the_recorder_binds_the_actor_once() -> None:
    """書き込み点ごとに帰属の詰め方が揺れないようにする。"""

    def clock() -> datetime:
        return datetime(2026, 9, 10, tzinfo=UTC)

    log = InMemoryAuditLog()
    recorder = AuditRecorder.for_user(
        log,
        tenant_id=TENANT,
        user_id=INSTRUCTOR,
        role="instructor",
        request_id="req-1",
        clock=clock,
    )

    recorder.record(
        AuditAction.GRADE_FINALIZED,
        target_type="submission",
        target_id="sub_1",
        summary="成績を確定した",
    )
    recorder.record(
        AuditAction.REVIEW_RECORDED,
        target_type="submission",
        target_id="sub_1",
        summary="採点を修正した",
    )

    rows = log.list_for_target("submission", "sub_1")
    assert len(rows) == 2
    assert {row.actor_user_id for row in rows} == {INSTRUCTOR}
    # 運用ログと突き合わせるための鍵が全行に載る（ADR 0016）。
    assert {row.request_id for row in rows} == {"req-1"}
    # id は自動採番。呼び出し側に振らせると重複と表記ゆれが出る。
    assert len({row.id for row in rows}) == 2


def test_the_system_recorder_cannot_be_given_an_actor() -> None:
    log = InMemoryAuditLog()
    recorder = AuditRecorder.for_system(log, tenant_id=TENANT)
    event = recorder.record(
        AuditAction.GRADE_FINALIZED,
        target_type="submission",
        target_id="sub_1",
        summary="締切を過ぎたので確定した",
    )
    assert event.actor_kind is ActorKind.SYSTEM
    assert event.actor_user_id is None


def test_a_failed_login_is_not_attributed_to_the_account_holder() -> None:
    """ログイン失敗は「その利用者が行ったこと」ではない。

    パスワードを間違えたのが本人とは限らず、**むしろ本人でない場合こそ
    記録が要る**。対象は口座、操作者は不明、と分けて書く。
    """
    with pytest.raises(ValidationError):
        _event(actor_kind=ActorKind.ANONYMOUS, actor_user_id=INSTRUCTOR)

    log = InMemoryAuditLog()
    recorder = AuditRecorder.for_anonymous(log, tenant_id=TENANT, source_ip="203.0.113.7")
    event = recorder.record(
        AuditAction.LOGIN_FAILED,
        target_type="user",
        target_id=str(INSTRUCTOR),
        summary="ログインに失敗した",
        detail={"login": "y239999"},
    )
    assert event.actor_kind is ActorKind.ANONYMOUS
    assert event.actor_user_id is None
    # 口座は分かっている。**やった人が分からない**ことと区別して書く。
    assert event.target_id == str(INSTRUCTOR)
    assert event.source_ip == "203.0.113.7"
