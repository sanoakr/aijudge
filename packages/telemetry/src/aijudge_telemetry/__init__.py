"""運用ログ（3 つあるログのうちの 1 つ）。

aiJudge のログは 3 つあり、混ぜない（ADR 0016）。

1. **運用ログ** — このパッケージ。プロセスに何が起きたか。journald に出て
   ローテートで消える。best-effort（ログの失敗で採点を止めない）。
2. **監査ログ** — `aijudge_audit`。**誰が**成績に関わる何をしたか。DB に
   append-only で残す。書けなければ操作ごと失敗させる。
3. **採点記録** — `GradingRun` の再現フィールド（ADR 0003）。どの版・
   どのモデルで採点したか。ログではなく採点の実体。

ここに載せてよいのは識別子だけで、学習者データは載せない（P7）。
"""

from __future__ import annotations

from .asgi import RequestContextMiddleware
from .context import ContextValueRejected, bind, current_context
from .logging_setup import (
    ENV_FORMAT,
    ENV_LEVEL,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    configured_service,
    redact_query,
    uvicorn_log_config,
)

__all__ = [
    "ENV_FORMAT",
    "ENV_LEVEL",
    "ContextValueRejected",
    "JsonFormatter",
    "RequestContextMiddleware",
    "TextFormatter",
    "bind",
    "configure_logging",
    "configured_service",
    "current_context",
    "redact_query",
    "uvicorn_log_config",
]
