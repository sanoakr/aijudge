"""インメモリの監査ログ。テストと、DB を持たない経路のため。

`packages/persistence` の実装と**同じテストに通る**ことが要件で、
両者が食い違ってよいのは並行性の扱いだけである。
"""

from __future__ import annotations

from datetime import datetime

from aijudge_core.ids import TenantId

from .event import AuditAction, AuditEvent
from .protocols import DuplicateAuditEvent


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._by_id: dict[str, AuditEvent] = {}

    def record(self, event: AuditEvent) -> None:
        if event.id in self._by_id:
            raise DuplicateAuditEvent(event.id)
        self._by_id[event.id] = event
        self._events.append(event)

    def find(self, event_id: str) -> AuditEvent | None:
        return self._by_id.get(event_id)

    def list_for_target(
        self, target_type: str, target_id: str, *, limit: int = 100
    ) -> tuple[AuditEvent, ...]:
        matched = [
            event
            for event in self._events
            if event.target_type == target_type and event.target_id == target_id
        ]
        return tuple(_newest_first(matched)[:limit])

    def list_recent(
        self,
        tenant_id: TenantId,
        *,
        action: AuditAction | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        matched = [
            event
            for event in self._events
            if event.tenant_id == tenant_id
            and (action is None or event.action is action)
            and (since is None or event.at >= since)
        ]
        return tuple(_newest_first(matched)[:limit])


def _newest_first(events: list[AuditEvent]) -> list[AuditEvent]:
    # 同時刻は投入順の逆。同じ操作から複数行出たときに順序が揺れないようにする。
    return sorted(events, key=lambda event: event.at, reverse=True)
