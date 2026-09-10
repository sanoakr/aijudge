"""監査ログの口。

実装は 2 つある ── インメモリ（`memory.py`）と PostgreSQL/SQLite
（`packages/persistence`）── で、`packages/persistence/tests` が**同じテストを
両方に通す**。この repo の他の Store / Repository と同じ約束である。

**書き込みは操作と同じトランザクションで行う。** `record` は commit しない。
呼び出し側の `UnitOfWork` が commit するので、操作が巻き戻れば監査行も
巻き戻り、監査行が書けなければ操作も成立しない。これが運用ログとの決定的な
違いである（ADR 0016）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aijudge_core.ids import TenantId

from .event import AuditAction, AuditEvent


class AuditLog(Protocol):
    """追記専用の監査記録。

    更新も削除も無い。**意図的に無い** ── 消せる記録は証拠にならない。
    保存期間で消すのは運用ログ側の話で、こちらは学期を跨いで残す
    （成績への異議申立てはその後に来る）。
    """

    def record(self, event: AuditEvent) -> None:
        """1 件書く。同じ `id` の再投入は拒否する。"""
        ...

    def find(self, event_id: str) -> AuditEvent | None: ...

    def list_for_target(
        self, target_type: str, target_id: str, *, limit: int = 100
    ) -> tuple[AuditEvent, ...]:
        """この対象に何が起きたかを新しい順に返す。

        「この提出の成績は誰がいつ確定したか」を引くための経路。
        """
        ...

    def list_recent(
        self,
        tenant_id: TenantId,
        *,
        action: AuditAction | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        """テナントの最近の行為を新しい順に返す。"""
        ...


class DuplicateAuditEvent(Exception):
    """同じ id の監査行を二度書こうとした。

    追記専用なので上書きは無い。**握り潰さない** ── ここで黙ると、
    記録したつもりの操作が記録されていない状態が生まれる。
    """
