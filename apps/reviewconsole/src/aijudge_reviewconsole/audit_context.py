"""要求から、監査ログに載せる文脈を取り出す。

**運用ログと監査ログを繋ぐのは合成ルートの仕事である**（ADR 0016）。
`aijudge_audit` は `aijudge_telemetry` を import しない ── 監査は運用ログが
無くても成立しなければならず、逆も同じ。両方を知っているのはここだけ。
"""

from __future__ import annotations

from fastapi import Request

from aijudge_audit import AuditRecorder
from aijudge_telemetry import client_ip, current_context


def request_id_of(request: Request) -> str | None:
    """`RequestContextMiddleware` が積んだ相関 ID。

    引数の `request` は使わないが、呼び出し側から見て「この要求の ID」で
    あることを署名で示すために受け取る。
    """
    value = current_context().get("request_id")
    return None if value is None else str(value)


def source_ip_of(request: Request) -> str | None:
    return client_ip(
        request.headers.get("x-forwarded-for"),
        None if request.client is None else request.client.host,
    )


def recorder_for(uow, request: Request, me, *, role: str | None = None) -> AuditRecorder:
    """この要求の操作者に紐づいた監査記録の口（ADR 0016）。

    **`uow.audit` を渡すのが要点。** 操作と同じトランザクションに載るので、
    操作が巻き戻れば監査行も巻き戻り、監査行が書けなければ操作も成立しない。

    役割の既定はテナント管理者かどうか。`/manage` の操作はコース単位の受講では
    なく管理権限で通っているので、そのときの権限をそのまま焼き込む
    （コースの役割が意味を持つ場面では `role` を明示して上書きする）。
    """
    return AuditRecorder.for_user(
        uow.audit,
        tenant_id=me.tenant_id,
        user_id=me.user_id,
        role=role if role is not None else ("tenant_admin" if me.is_tenant_admin else None),
        request_id=request_id_of(request),
        source_ip=source_ip_of(request),
    )
