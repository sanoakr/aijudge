"""ブラウザ IDE（`docs/design/online-coding-test.md`）。

段階 1 で持つのは**試しの実行**の要求・キュー・受付の検査だけである
（ADR 0024）。画面・提出・行動記録は後の段階で足す。

**このパッケージはコードを動かさない。** `aijudge_sandbox` を import しない
ことは `.importlinter` の `ide-does-not-run-code` が固定している ── 動かすのは
合成ルートの runner（`apps/runner`）だけで、web には Docker の権限を与えない
（不変条件 I6）。
"""

from __future__ import annotations

from .activity import (
    EVENT_TYPES,
    MAX_BATCH_BYTES,
    ActivityFiles,
    ActivityIndex,
    ActivityRejected,
    EventBatch,
    IdeSession,
    IdeSessionId,
    check_events,
    check_snapshots,
    snapshot_name,
)
from .buffer import (
    BufferStore,
    BufferTooLarge,
    IdeBuffer,
    content_hash,
    make_buffer,
)
from .flags import FLAG_LABELS, Flag, FlagKind, flag_events, submission_mismatches
from .formats import EDITOR_FORMATS, EditorFormat, editor_formats
from .intake import RefusalReason, RunPolicy, RunRefused, RunView, request_run, view_run
from .integrity import SILENCE_MS, IntegrityReport, Silence, check_session
from .links import SubmissionLink, SubmissionLinkStore, SubmissionOrigin
from .memory import (
    InMemoryActivityIndex,
    InMemoryBufferStore,
    InMemoryRunQueue,
    InMemorySubmissionLinkStore,
)
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
from .summary import ActivitySummary, summarize

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DISPLAY_OUTPUT_CHARS",
    "EDITOR_FORMATS",
    "EVENT_TYPES",
    "FLAG_LABELS",
    "IN_FLIGHT_STATES",
    "MAX_BATCH_BYTES",
    "MAX_SOURCE_BYTES",
    "MAX_STDIN_BYTES",
    "RUNNER_LOST",
    "SILENCE_MS",
    "STALE_AFTER_SECONDS",
    "TERMINAL_STATES",
    "ActivityFiles",
    "ActivityIndex",
    "ActivityRejected",
    "ActivitySummary",
    "BufferStore",
    "BufferTooLarge",
    "EditorFormat",
    "EventBatch",
    "Flag",
    "FlagKind",
    "IdeBuffer",
    "IdeSession",
    "IdeSessionId",
    "InMemoryActivityIndex",
    "InMemoryBufferStore",
    "InMemoryRunQueue",
    "InMemorySubmissionLinkStore",
    "IntegrityReport",
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
    "Silence",
    "SubmissionLink",
    "SubmissionLinkStore",
    "SubmissionOrigin",
    "check_events",
    "check_session",
    "check_snapshots",
    "clip_for_display",
    "content_hash",
    "editor_formats",
    "flag_events",
    "make_buffer",
    "request_run",
    "snapshot_name",
    "submission_mismatches",
    "summarize",
    "view_run",
]
