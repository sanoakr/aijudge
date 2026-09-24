"""IDE の自動保存の SQLAlchemy 実装（設計書 §6.5）。

インメモリ実装（`aijudge_ide.InMemoryBufferStore`）と**同じ規則**を守る。
`packages/persistence/tests/test_buffer_repository.py` が同じテストを両方に通す。

commit しない。呼び出し側の UnitOfWork が commit する。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aijudge_core.ids import TaskId, TenantId, UserId
from aijudge_ide import IdeBuffer

from .schema import IdeBufferRow


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
