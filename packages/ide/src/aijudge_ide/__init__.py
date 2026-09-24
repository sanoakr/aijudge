"""ブラウザ IDE（`docs/design/online-coding-test.md`）。

段階 1 で持つのは**試しの実行**の要求・キュー・受付の検査だけである
（ADR 0024）。画面・提出・行動記録は後の段階で足す。

**このパッケージはコードを動かさない。** `aijudge_sandbox` を import しない
ことは `.importlinter` の `ide-does-not-run-code` が固定している ── 動かすのは
合成ルートの runner（`apps/runner`）だけで、web には Docker の権限を与えない
（不変条件 I6）。
"""

from __future__ import annotations

from .buffer import (
    BufferStore,
    BufferTooLarge,
    IdeBuffer,
    content_hash,
    make_buffer,
)
from .formats import EDITOR_FORMATS, EditorFormat, editor_formats
from .intake import RefusalReason, RunPolicy, RunRefused, RunView, request_run, view_run
from .memory import InMemoryBufferStore, InMemoryRunQueue
from .protocols import RunAlreadyPending, RunQueue
from .run import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DISPLAY_OUTPUT_CHARS,
    IN_FLIGHT_STATES,
    MAX_SOURCE_BYTES,
    MAX_STDIN_BYTES,
    RUNNER_LOST,
    STALE_AFTER_SECONDS,
    TERMINAL_STATES,
    RunOutcome,
    RunRequest,
    RunRequestId,
    RunStage,
    RunState,
    clip_for_display,
)

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DISPLAY_OUTPUT_CHARS",
    "EDITOR_FORMATS",
    "IN_FLIGHT_STATES",
    "MAX_SOURCE_BYTES",
    "MAX_STDIN_BYTES",
    "RUNNER_LOST",
    "STALE_AFTER_SECONDS",
    "TERMINAL_STATES",
    "BufferStore",
    "BufferTooLarge",
    "EditorFormat",
    "IdeBuffer",
    "InMemoryBufferStore",
    "InMemoryRunQueue",
    "RefusalReason",
    "RunAlreadyPending",
    "RunOutcome",
    "RunPolicy",
    "RunQueue",
    "RunRefused",
    "RunRequest",
    "RunRequestId",
    "RunStage",
    "RunState",
    "RunView",
    "clip_for_display",
    "content_hash",
    "editor_formats",
    "make_buffer",
    "request_run",
    "view_run",
]
