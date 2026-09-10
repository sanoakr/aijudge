"""要求から、監査ログに載せる文脈を取り出す。

**運用ログと監査ログを繋ぐのは合成ルートの仕事である**（ADR 0016）。
`aijudge_audit` は `aijudge_telemetry` を import しない ── 監査は運用ログが
無くても成立しなければならず、逆も同じ。両方を知っているのはここだけ。
"""

from __future__ import annotations

from fastapi import Request

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
