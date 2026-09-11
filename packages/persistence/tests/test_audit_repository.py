"""監査ログの 2 つの実装が同じ規則を守ること（ADR 0016）。

**同じテストを両方に当てる。** 片方だけ通る規則は、移行したときに初めて
破綻する。ここで確かめるのは 4 つ。

- 追記専用（同じ id を二度書けない）
- 帰属を偽らない（自動処理を人に付け替えない）
- **操作と同じトランザクションに載る**（巻き戻れば消え、書けなければ操作も失敗）
- 後から引ける（対象別・テナント別・行為別）
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_audit import (
    ActorKind,
    AuditAction,
    AuditEvent,
    AuditLog,
    AuditRecorder,
    DuplicateAuditEvent,
    InMemoryAuditLog,
)
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)
INSTRUCTOR = UserId("usr_" + "1" * 32)
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Iterator[Database]:
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def an_event(**overrides) -> AuditEvent:
    base = {
        "id": "aud_" + "a" * 32,
        "at": NOW,
        "tenant_id": TENANT,
        "actor_kind": ActorKind.USER,
        "actor_user_id": INSTRUCTOR,
        "actor_role": "instructor",
        "action": AuditAction.GRADE_FINALIZED,
        "target_type": "submission",
        "target_id": "sub_" + "2" * 32,
        "summary": "成績を確定した",
        "detail": {"total": {"before": None, "after": 82}},
        "request_id": "req-1",
    }
    return AuditEvent(**{**base, **overrides})


# 両方の実装に同じテストを当てるための入口。SQL 側は UnitOfWork の中でしか
# 使えないので、テスト本体は「開いた log」を受け取る形にしてある。
def _both(database: Database):
    yield InMemoryAuditLog()
    with database.unit_of_work() as uow:
        yield uow.audit


def test_a_row_can_be_read_back_whole(database: Database) -> None:
    for log in _both(database):
        log.record(an_event())
        found = log.find("aud_" + "a" * 32)
        assert found is not None
        assert found.action is AuditAction.GRADE_FINALIZED
        assert found.actor_user_id == INSTRUCTOR
        assert found.actor_role == "instructor"
        assert found.detail == {"total": {"before": None, "after": 82}}
        # 運用ログと突き合わせるための鍵（ADR 0016）。
        assert found.request_id == "req-1"


def test_the_same_row_cannot_be_written_twice(database: Database) -> None:
    """追記専用。上書きの経路を作らない ── 消せる記録は証拠にならない。"""
    for log in _both(database):
        log.record(an_event())
        with pytest.raises(DuplicateAuditEvent):
            log.record(an_event(summary="書き換えたい"))


def test_a_system_action_stays_unattributed(database: Database) -> None:
    """締切経過による自動確定に操作者はいない。誰かに付け替えない。"""
    for log in _both(database):
        log.record(an_event(actor_kind=ActorKind.SYSTEM, actor_user_id=None, actor_role=None))
        found = log.find("aud_" + "a" * 32)
        assert found is not None
        assert found.actor_kind is ActorKind.SYSTEM
        assert found.actor_user_id is None


def test_what_happened_to_this_submission(database: Database) -> None:
    target = "sub_" + "2" * 32
    for log in _both(database):
        log.record(an_event(id="aud_" + "1" * 32, at=NOW, action=AuditAction.REVIEW_RECORDED))
        log.record(an_event(id="aud_" + "2" * 32, at=NOW + timedelta(hours=1)))
        log.record(an_event(id="aud_" + "3" * 32, target_id="sub_" + "8" * 32))

        rows = log.list_for_target("submission", target)
        assert [row.action for row in rows] == [
            AuditAction.GRADE_FINALIZED,
            AuditAction.REVIEW_RECORDED,
        ]


def test_recent_rows_narrow_by_tenant_action_and_time(database: Database) -> None:
    for log in _both(database):
        log.record(an_event(id="aud_" + "1" * 32, at=NOW, action=AuditAction.LOGIN_FAILED))
        log.record(an_event(id="aud_" + "2" * 32, at=NOW + timedelta(days=1)))
        log.record(an_event(id="aud_" + "3" * 32, tenant_id=OTHER_TENANT))

        assert len(log.list_recent(TENANT)) == 2
        assert len(log.list_recent(OTHER_TENANT)) == 1
        assert [r.id for r in log.list_recent(TENANT, action=AuditAction.LOGIN_FAILED)] == [
            "aud_" + "1" * 32
        ]
        assert [r.id for r in log.list_recent(TENANT, since=NOW + timedelta(hours=1))] == [
            "aud_" + "2" * 32
        ]


def test_the_limit_is_honoured(database: Database) -> None:
    for log in _both(database):
        for index in range(5):
            log.record(an_event(id=f"aud_{index:032d}", at=NOW + timedelta(minutes=index)))
        assert len(log.list_recent(TENANT, limit=2)) == 2
        assert len(log.list_for_target("submission", "sub_" + "2" * 32, limit=3)) == 3


# -- ここから下は SQL 実装にしか無い性質（トランザクション）----------------


def test_the_row_rolls_back_with_the_operation(database: Database) -> None:
    """**監査ログは操作と同じトランザクションに載る。**

    操作が巻き戻れば監査行も巻き戻る。別の接続で書くと、失敗した操作の
    記録だけが残る ── それは起きなかったことの記録であり、監査を汚す。
    """
    with database.unit_of_work() as uow:
        uow.audit.record(an_event())
        # commit しないまま抜ける（`SqlUnitOfWork.__exit__` が rollback する）。

    with database.unit_of_work() as uow:
        assert uow.audit.find("aud_" + "a" * 32) is None


def test_the_row_survives_a_commit(database: Database) -> None:
    with database.unit_of_work() as uow:
        uow.audit.record(an_event())
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.audit.find("aud_" + "a" * 32) is not None


def test_the_recorder_works_against_either_implementation(database: Database) -> None:
    """`AuditRecorder` は保存先を知らない（プロトコルにしか触らない）。"""
    logs: list[AuditLog] = [InMemoryAuditLog()]
    with database.unit_of_work() as uow:
        logs.append(uow.audit)
        for log in logs:
            recorder = AuditRecorder.for_user(
                log,
                tenant_id=TENANT,
                user_id=INSTRUCTOR,
                role="instructor",
                request_id="req-2",
                clock=lambda: NOW,
            )
            event = recorder.record(
                AuditAction.REVIEW_RECORDED,
                target_type="submission",
                target_id="sub_" + "2" * 32,
                summary="採点を修正した",
            )
            assert log.find(event.id) is not None


def test_an_enrolment_target_fits(database: Database) -> None:
    """**対象が対の記録も保存できること。**

    受講登録には固有の id が無く、「どのコースの誰か」の対でしか名指せない
    （`crs_…:usr_…` で 73 字）。列が 64 字だったころ、デモコースの自動登録が
    最初のログインで 500 を返した ── 記録が入らないので、ログインの
    トランザクションごと巻き戻っていた。
    """
    target = f"crs_{'1' * 32}:usr_{'2' * 32}"
    assert len(target) == 73

    for log in _both(database):
        log.record(
            an_event(
                action=AuditAction.ENROLLED,
                target_type="enrolment",
                target_id=target,
                summary="デモコースに自動登録した（learner）",
            )
        )
        assert [event.target_id for event in log.list_for_target("enrolment", target)] == [target]
