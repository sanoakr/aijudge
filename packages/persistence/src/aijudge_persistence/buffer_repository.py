"""IDE の自動保存と提出の出どころの SQLAlchemy 実装（設計書 §6.5・§9）。

インメモリ実装（`aijudge_ide.InMemoryBufferStore`）と**同じ規則**を守る。
`packages/persistence/tests/test_buffer_repository.py` が同じテストを両方に通す。

commit しない。呼び出し側の UnitOfWork が commit する。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aijudge_core.ids import SubmissionId, TaskId, TenantId, UserId
from aijudge_ide import IdeBuffer, SubmissionLink, SubmissionOrigin

from .schema import IdeBufferRow, IdeSubmissionLinkRow


def _buffer(row: IdeBufferRow) -> IdeBuffer:
    return IdeBuffer(
        tenant_id=TenantId(row.tenant_id),
        learner_id=UserId(row.learner_id),
        task_id=TaskId(row.task_id),
        suffix=row.suffix,
        source=row.source,
        updated_at=row.updated_at,
        content_hash=row.content_hash,
    )


class SqlBufferStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, buffer: IdeBuffer) -> None:
        row = self._session.get(IdeBufferRow, (str(buffer.learner_id), str(buffer.task_id)))
        if row is None:
            row = IdeBufferRow(learner_id=str(buffer.learner_id), task_id=str(buffer.task_id))
            self._session.add(row)
        row.tenant_id = str(buffer.tenant_id)
        row.suffix = buffer.suffix
        row.source = buffer.source
        row.content_hash = buffer.content_hash
        row.updated_at = buffer.updated_at
        self._session.flush()

    def get(self, learner_id: UserId, task_id: TaskId) -> IdeBuffer | None:
        row = self._session.get(IdeBufferRow, (str(learner_id), str(task_id)))
        return None if row is None else _buffer(row)

    def for_tasks(self, learner_id: UserId, task_ids: list[TaskId]) -> dict[TaskId, IdeBuffer]:
        if not task_ids:
            return {}
        rows = (
            self._session.execute(
                select(IdeBufferRow).where(
                    IdeBufferRow.learner_id == str(learner_id),
                    IdeBufferRow.task_id.in_([str(t) for t in task_ids]),
                )
            )
            .scalars()
            .all()
        )
        return {TaskId(row.task_id): _buffer(row) for row in rows}

    def for_task(self, task_id: TaskId) -> tuple[IdeBuffer, ...]:
        rows = (
            self._session.execute(
                select(IdeBufferRow)
                .where(IdeBufferRow.task_id == str(task_id))
                .order_by(IdeBufferRow.learner_id)
            )
            .scalars()
            .all()
        )
        return tuple(_buffer(row) for row in rows)

    def task_ids(self) -> tuple[TaskId, ...]:
        rows = self._session.execute(
            select(IdeBufferRow.task_id).distinct().order_by(IdeBufferRow.task_id)
        ).scalars()
        return tuple(TaskId(task_id) for task_id in rows)


class SqlSubmissionLinkStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, link: SubmissionLink) -> None:
        # **最初の出どころを残す**（プロトコルの docstring）。同じ提出に 2 度目は書かない。
        if self._session.get(IdeSubmissionLinkRow, str(link.submission_id)) is not None:
            return
        self._session.add(
            IdeSubmissionLinkRow(
                submission_id=str(link.submission_id),
                tenant_id=str(link.tenant_id),
                learner_id=str(link.learner_id),
                task_id=str(link.task_id),
                origin=link.origin.value,
                content_hash=link.content_hash,
                ide_session_id=link.ide_session_id,
                recorded_at=link.recorded_at,
            )
        )
        self._session.flush()

    def for_submission(self, submission_id: SubmissionId) -> SubmissionLink | None:
        row = self._session.get(IdeSubmissionLinkRow, str(submission_id))
        if row is None:
            return None
        return SubmissionLink(
            submission_id=SubmissionId(row.submission_id),
            tenant_id=TenantId(row.tenant_id),
            learner_id=UserId(row.learner_id),
            task_id=TaskId(row.task_id),
            origin=SubmissionOrigin(row.origin),
            content_hash=row.content_hash,
            ide_session_id=row.ide_session_id,
            recorded_at=row.recorded_at,
        )
