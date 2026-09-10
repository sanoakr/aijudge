"""操作者を束ねて 1 行を書く。

素の `AuditEvent` を毎回組み立てさせると、書き込み点ごとに `tenant_id` や
役割の詰め方が揺れる ── そして揺れたところだけ後から引けなくなる。
操作者は 1 リクエストの中で変わらないので、そこで 1 度だけ束ねる。

**`request_id` は引数で受け取る。** 運用ログ（`aijudge_telemetry`）を
import して取りに行かない ── 監査は運用ログが無くても成立しなければならず、
逆向きも同じ。繋ぐのは合成ルート（`apps/*`）の仕事である（ADR 0016）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aijudge_core.ids import TenantId, UserId, new_id

from .event import ActorKind, AuditAction, AuditEvent
from .protocols import AuditLog


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditRecorder:
    """1 人の操作者（または 1 つの自動処理）が行うことを記録する。"""

    def __init__(
        self,
        log: AuditLog,
        *,
        tenant_id: TenantId,
        actor_kind: ActorKind,
        actor_user_id: UserId | None = None,
        actor_role: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._log = log
        self._tenant_id = tenant_id
        self._actor_kind = actor_kind
        self._actor_user_id = actor_user_id
        self._actor_role = actor_role
        self._request_id = request_id
        self._source_ip = source_ip
        self._clock = clock

    @classmethod
    def for_user(
        cls,
        log: AuditLog,
        *,
        tenant_id: TenantId,
        user_id: UserId,
        role: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> AuditRecorder:
        return cls(
            log,
            tenant_id=tenant_id,
            actor_kind=ActorKind.USER,
            actor_user_id=user_id,
            actor_role=role,
            request_id=request_id,
            source_ip=source_ip,
            clock=clock,
        )

    @classmethod
    def for_system(
        cls,
        log: AuditLog,
        *,
        tenant_id: TenantId,
        request_id: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> AuditRecorder:
        """人間の操作者がいない処理（締切経過による自動確定など）。

        **利用者に帰属させない。** 帰属させると、教員が確定していないものを
        確定したことにしてしまう（ADR 0010 が分けた区別が、監査の側で壊れる）。
        """
        return cls(
            log,
            tenant_id=tenant_id,
            actor_kind=ActorKind.SYSTEM,
            request_id=request_id,
            clock=clock,
        )

    @classmethod
    def for_anonymous(
        cls,
        log: AuditLog,
        *,
        tenant_id: TenantId,
        request_id: str | None = None,
        source_ip: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> AuditRecorder:
        """認証されていない誰かの試み（ログイン失敗など）。

        **口座の持ち主に帰属させない。** パスワードを間違えたのが本人とは
        限らず、むしろ本人でない場合こそ記録が要る。対象は `target_id` に書く。
        """
        return cls(
            log,
            tenant_id=tenant_id,
            actor_kind=ActorKind.ANONYMOUS,
            request_id=request_id,
            source_ip=source_ip,
            clock=clock,
        )

    def record(
        self,
        action: AuditAction,
        *,
        target_type: str,
        target_id: str,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=new_id("aud"),
            at=self._clock(),
            tenant_id=self._tenant_id,
            actor_kind=self._actor_kind,
            actor_user_id=self._actor_user_id,
            actor_role=self._actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            summary=summary,
            detail=detail or {},
            request_id=self._request_id,
            source_ip=self._source_ip,
        )
        self._log.record(event)
        return event
