"""Outbox のリレー。

提出と同じトランザクションで記録されたイベントを、購読側へ流す。
Phase 0 は購読側がプロセス内にしか無いので、ここは「未送信を読んで
ハンドラを呼び、送信済みにする」だけ。Redis Streams に載せ替えても
形は変わらない（設計方針 §2.3）。

**購読側は冪等でなければならない。** 送信済みにする前にプロセスが落ちれば
同じイベントが再送される。「1 回だけ届く」は作れないので、
「何回届いても同じ」を購読側に要求する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from aijudge_core.events import DomainEvent
from aijudge_persistence import Database

logger = logging.getLogger(__name__)

Handler = Callable[[DomainEvent], None]


class EventRelay:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._handlers: dict[str, list[Handler]] = {}

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def drain(self, limit: int = 100) -> int:
        """未送信のイベントを流す。流した件数を返す。

        ハンドラが落ちたイベントは**送信済みにしない**。次回に再送される。
        落ちたものを送信済みにすると、そのイベントは永久に失われる。
        """
        with self._database.unit_of_work() as uow:
            events = uow.outbox.unpublished(limit)

        delivered: list[str] = []
        for event in events:
            if self._deliver(event):
                delivered.append(str(event.event_id))

        if delivered:
            with self._database.unit_of_work() as uow:
                uow.outbox.mark_published(delivered)
                uow.commit()
        return len(delivered)

    def _deliver(self, event: DomainEvent) -> bool:
        handlers: Sequence[Handler] = self._handlers.get(event.type, ())
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("handler failed for %s %s", event.type, event.event_id)
                return False
        return True
