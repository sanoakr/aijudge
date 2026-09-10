"""運用ログの設定。**合成ルート（apps/*）だけが呼ぶ。**

各パッケージは `logging.getLogger(__name__)` を使うだけでよい。
ハンドラとフォーマットを決めるのはプロセスを組み立てる側の責務であり、
ライブラリ側が決めると、同じコードが取り込まれ方によって違う出方をする。

**そうなっていた。** `basicConfig` はワーカーと finalize の 2 つにしか無く、
`aijudge-web` / `aijudge-review` では同じ `logger.warning` が root の
lastResort（WARNING 固定・書式なし・stderr）に落ちて、INFO は捨てられていた。

出力は 1 行 1 イベントの JSON（`AIJUDGE_LOG_FORMAT=json`）。運用では journald が
受け、`journalctl -o cat -u aijudge-worker-det | jq` で読む。開発では人が読むので
既定は text。

ここは **best-effort の層** である。ログの失敗で採点を止めてはいけない
（監査ログは逆で、書けなければ操作ごと失敗させる ── ADR 0016）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, TextIO

from .context import current_context

ENV_LEVEL = "AIJUDGE_LOG_LEVEL"
ENV_FORMAT = "AIJUDGE_LOG_FORMAT"

DEFAULT_LEVEL = "INFO"
DEFAULT_FORMAT = "text"

# LogRecord が必ず持つ属性。これ以外は呼び出し側が `extra=` で足した値なので、
# そのままイベントの欄として出す。
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# **INFO で URL を丸ごと吐くライブラリ。** 既定で WARNING に落とす。
# `httpx` は「HTTP Request: GET https://... 200 OK」を INFO で出すので、
# OIDC のトークン交換（`packages/identity/.../oidc.py`）を有効にした途端、
# 認可コードやクエリの秘密が journald に平文で残る ── ログに載せてよいのは
# 識別子だけ、という約束（P7）を、こちらが何も書かなくても破る側にいる。
# 追いたいときは `AIJUDGE_LOG_LEVEL=DEBUG` ではなく、個別に段を上げること。
NOISY_LOGGERS = ("httpx", "httpcore", "urllib3")

# 設定済みかどうか。uvicorn の worker は同じプロセスで何度も組み立てるので、
# 呼ぶたびにハンドラが増えないようにする（ログが 2 重・3 重に出る典型）。
_configured_service: str | None = None


class JsonFormatter(logging.Formatter):
    """1 行 1 イベントの JSON。

    欄は固定 4 つ（`ts` `level` `logger` `service`）＋ メッセージ ＋ 文脈 ＋
    `extra`。例外は `error` にまとめる（`traceback` の改行は JSON が畳むので
    1 行のまま）。
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        event.update(current_context())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                event[key] = value
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            event["error"] = {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": str(exc_value),
                "traceback": self.formatException(record.exc_info),
            }
        # 落とさない。ログの整形で例外を投げると、報告したかった障害の方が消える。
        return json.dumps(event, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """開発機で人が読む形。文脈は `key=value` で末尾に付ける。"""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        context = dict(current_context())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                context[key] = value
        if context:
            line = f"{line} [{' '.join(f'{k}={v}' for k, v in context.items())}]"
        return line


def build_formatter(service: str, fmt: str) -> logging.Formatter:
    return JsonFormatter(service) if fmt == "json" else TextFormatter()


def configure_logging(
    service: str,
    *,
    level: str | None = None,
    fmt: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """このプロセスのログを 1 つの形に決める。**main の先頭で 1 度だけ呼ぶ。**

    `level` と `fmt` は引数 → 環境変数 → 既定 の順に決まる。運用では
    `EnvironmentFile` から `AIJUDGE_LOG_FORMAT=json` を渡す。

    2 度目以降の呼び出しはハンドラを付け替える（増やさない）。
    """
    global _configured_service

    resolved_level = (level or os.environ.get(ENV_LEVEL) or DEFAULT_LEVEL).upper()
    resolved_format = (fmt or os.environ.get(ENV_FORMAT) or DEFAULT_FORMAT).lower()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(build_formatter(service, resolved_format))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved_level, logging.INFO))
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured_service = service


def configured_service() -> str | None:
    """設定済みならサービス名。テストと診断用。"""
    return _configured_service


def uvicorn_log_config(
    service: str, *, level: str | None = None, fmt: str | None = None
) -> dict[str, Any]:
    """uvicorn に渡す `log_config`。uvicorn 自身のログを同じ形に載せる。

    **アクセスログはこれまで存在しなかった。** `uvicorn.run(log_level="warning")` は
    `uvicorn.access`（INFO で出る）ごと黙らせるので、誰がいつ何を開いたかの記録が
    どこにも残っていなかった。

    ただしアクセスログを出すのは `RequestContextMiddleware` の側で、**uvicorn
    自身のアクセスログは止める**（`uvicorn.access` は WARNING 止まり）。理由は 2 つ。

    - uvicorn の既定書式はパスとクエリを一体で持つので、`?token=` の類が
      平文でログに残る。
    - uvicorn は `request_id` を知らない。相関 ID の載らないアクセスログは
      ワーカー側のログと突き合わせられず、それではそもそもの動機を満たさない。
    """
    resolved_level = (level or os.environ.get(ENV_LEVEL) or DEFAULT_LEVEL).upper()
    resolved_format = (fmt or os.environ.get(ENV_FORMAT) or DEFAULT_FORMAT).lower()
    formatter = (
        {"()": "aijudge_telemetry.logging_setup.JsonFormatter", "service": service}
        if resolved_format == "json"
        else {"()": "aijudge_telemetry.logging_setup.TextFormatter"}
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"aijudge": formatter},
        "handlers": {
            "aijudge": {
                "class": "logging.StreamHandler",
                "formatter": "aijudge",
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["aijudge"], "level": resolved_level, "propagate": False},
            "uvicorn.error": {"handlers": ["aijudge"], "level": resolved_level, "propagate": False},
            # 二重に出さない（上の docstring を参照）。
            "uvicorn.access": {"handlers": ["aijudge"], "level": "WARNING", "propagate": False},
        },
        "root": {"handlers": ["aijudge"], "level": resolved_level},
    }


def redact_query(target: str) -> str:
    """URL からクエリ文字列を落とす。アクセスログに残してよいのは経路だけ。"""
    return target.split("?", 1)[0]
