"""監査ログの SQLAlchemy 実装（ADR 0016）。

インメモリ実装（`aijudge_audit.InMemoryAuditLog`）と**同じ規則**を守る。
`packages/persistence/tests/test_audit_repository.py` が同じテストを両方に
通す ── この repo の他の Store / Repository と同じ約束である。

**`record` は commit しない。** 呼び出し側の `UnitOfWork` が commit するので、
操作が巻き戻れば監査行も巻き戻り、監査行が書けなければ操作も成立しない。
運用ログとの決定的な違いはここで、意図的にそうしてある。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aijudge_audit import ActorKind, AuditAction, AuditEvent, DuplicateAuditEvent
from aijudge_core.ids import TenantId, UserId

from .schema import AuditEventRow


def _to_event(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        at=row.at,
        tenant_id=TenantId(row.tenant_id),
        actor_kind=ActorKind(row.actor_kind),
        actor_user_id=None if row.actor_user_id is None else UserId(row.actor_user_id),
        actor_role=row.actor_role,
        action=AuditAction(row.action),
        target_type=row.target_type,
        target_id=row.target_id,
        summary=row.summary,
        detail=row.detail or {},
        request_id=row.request_id,
        source_ip=row.source_ip,
    )


class SqlAuditLog:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, event: AuditEvent) -> None:
        if self._session.get(AuditEventRow, event.id) is not None:
            # 追記専用なので上書きは無い。**握り潰さない** ── ここで黙ると、
            # 記録したつもりの操作が記録されていない状態が生まれる。
            raise DuplicateAuditEvent(event.id)
        self._session.add(
            AuditEventRow(
                id=event.id,
                at=event.at,
                tenant_id=str(event.tenant_id),
                actor_kind=event.actor_kind.value,
                actor_user_id=None if event.actor_user_id is None else str(event.actor_user_id),
                actor_role=event.actor_role,
                action=event.action.value,
                target_type=event.target_type,
                target_id=event.target_id,
                summary=event.summary,
                detail=dict(event.detail),
                request_id=event.request_id,
                source_ip=event.source_ip,
            )
        )
        # 同じ操作の中で書いた行をすぐ引けるようにする（commit はしない）。
        self._session.flush()

    def find(self, event_id: str) -> AuditEvent | None:
        row = self._session.get(AuditEventRow, event_id)
        return None if row is None else _to_event(row)

    def list_for_target(
        self, target_type: str, target_id: str, *, limit: int = 100
    ) -> tuple[AuditEvent, ...]:
        rows = (
            self._session.execute(
                select(AuditEventRow)
                .where(
                    AuditEventRow.target_type == target_type,
                    AuditEventRow.target_id == target_id,
                )
                .order_by(AuditEventRow.at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return tuple(_to_event(row) for row in rows)

    def list_recent(
        self,
        tenant_id: TenantId,
        *,
        action: AuditAction | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        statement = select(AuditEventRow).where(AuditEventRow.tenant_id == str(tenant_id))
        if action is not None:
            statement = statement.where(AuditEventRow.action == action.value)
        if since is not None:
            statement = statement.where(AuditEventRow.at >= since)
        rows = (
            self._session.execute(statement.order_by(AuditEventRow.at.desc()).limit(limit))
            .scalars()
            .all()
        )
        return tuple(_to_event(row) for row in rows)
