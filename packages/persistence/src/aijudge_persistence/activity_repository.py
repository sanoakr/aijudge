"""行動記録の索引の SQLAlchemy 実装（ADR 0023）。

インメモリ実装（`aijudge_ide.InMemoryActivityIndex`）と**同じ規則**を守る。
`packages/persistence/tests/test_activity_repository.py` が同じテストを両方に通す。

commit しない。呼び出し側の UnitOfWork が commit する。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_ide import EventBatch, IdeSession, IdeSessionId

from .schema import IdeEventBatchRow, IdeSessionRow


class SqlActivityIndex:
    def __init__(self, session: Session) -> None:
        self._session = session

    def start_session(self, session: IdeSession) -> None:
        if self._session.get(IdeSessionRow, str(session.id)) is not None:
            raise ValueError(f"ide session {session.id} already exists")
        self._session.add(
            IdeSessionRow(
                id=str(session.id),
                tenant_id=str(session.tenant_id),
                learner_id=str(session.learner_id),
                course_id=str(session.course_id),
                unit=session.unit,
                started_at=session.started_at,
                user_agent=session.user_agent,
                consented_at=session.consented_at,
            )
        )
        self._session.flush()

    def get_session(self, session_id: IdeSessionId) -> IdeSession | None:
        row = self._session.get(IdeSessionRow, str(session_id))
        if row is None:
            return None
        return IdeSession(
            id=IdeSessionId(row.id),
            tenant_id=TenantId(row.tenant_id),
            learner_id=UserId(row.learner_id),
            course_id=CourseId(row.course_id),
            unit=row.unit,
            started_at=row.started_at,
            user_agent=row.user_agent,
            consented_at=row.consented_at,
        )

    def delete_for_course(self, course_id: CourseId) -> tuple[IdeSession, ...]:
        rows = (
            self._session.execute(
                select(IdeSessionRow.id).where(IdeSessionRow.course_id == str(course_id))
            )
            .scalars()
            .all()
        )
        doomed = tuple(
            session
            for session in (self.get_session(IdeSessionId(row)) for row in rows)
            if session is not None
        )
        if rows:
            self._session.execute(
                delete(IdeEventBatchRow).where(IdeEventBatchRow.ide_session_id.in_(list(rows)))
            )
            self._session.execute(
                delete(IdeSessionRow).where(IdeSessionRow.course_id == str(course_id))
            )
            self._session.flush()
        return doomed

    def sessions_for(self, learner_id: UserId, course_id: CourseId) -> tuple[IdeSession, ...]:
        rows = (
            self._session.execute(
                select(IdeSessionRow.id)
                .where(
                    IdeSessionRow.learner_id == str(learner_id),
                    IdeSessionRow.course_id == str(course_id),
                )
                .order_by(IdeSessionRow.started_at, IdeSessionRow.id)
            )
            .scalars()
            .all()
        )
        return tuple(
            session
            for session in (self.get_session(IdeSessionId(row)) for row in rows)
            if session is not None
        )

    def has_consented(self, learner_id: UserId, course_id: CourseId) -> bool:
        found = self._session.execute(
            select(IdeSessionRow.id)
            .where(
                IdeSessionRow.learner_id == str(learner_id),
                IdeSessionRow.course_id == str(course_id),
            )
            .limit(1)
        ).first()
        return found is not None

    def add_batch(self, batch: EventBatch) -> bool:
        key = (str(batch.ide_session_id), batch.seq)
        if self._session.get(IdeEventBatchRow, key) is not None:
            return False
        self._session.add(
            IdeEventBatchRow(
                ide_session_id=str(batch.ide_session_id),
                seq=batch.seq,
                received_at=batch.received_at,
                client_time=batch.client_time,
                event_count=batch.event_count,
                snapshot_count=batch.snapshot_count,
                byte_size=batch.byte_size,
                sha256=batch.sha256,
                path=batch.path,
            )
        )
        self._session.flush()
        return True

    def batches(self, session_id: IdeSessionId) -> tuple[EventBatch, ...]:
        rows = (
            self._session.execute(
                select(IdeEventBatchRow)
                .where(IdeEventBatchRow.ide_session_id == str(session_id))
                .order_by(IdeEventBatchRow.seq)
            )
            .scalars()
            .all()
        )
        return tuple(
            EventBatch(
                ide_session_id=IdeSessionId(row.ide_session_id),
                seq=row.seq,
                received_at=row.received_at,
                client_time=row.client_time,
                event_count=row.event_count,
                snapshot_count=row.snapshot_count,
                byte_size=row.byte_size,
                sha256=row.sha256,
                path=row.path,
            )
            for row in rows
        )
