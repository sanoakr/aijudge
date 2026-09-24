"""実行要求の受付（設計書 §8.1）。

web が「実行」の要求を受けたときに通す検査。**ここに来る前に、提出と同じ
関門（学内限定・受付期間・役割・`may_see`）を web 側で通してある前提**で、
ここが見るのは実行に固有の条件だけである。

- ソースと標準入力の大きさ
- 1 人が同時に待てるのは 1 件
- 同じ学習者の連続実行の間隔

関門をここに持ってこないのは、関門が `/submit` と**同じ関数**でなければ
ならないから（不変条件 I8、#119・#370）。ここに写しを持つと、写しから
条件が漏れる ── 実際に動画の経路で学内限定が漏れた。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from aijudge_core.ids import TaskVersionId, TenantId, UserId, new_id

from .protocols import RunAlreadyPending, RunQueue
from .run import (
    DEFAULT_COOLDOWN_SECONDS,
    MAX_SOURCE_BYTES,
    MAX_STDIN_BYTES,
    STALE_AFTER_SECONDS,
    RunRequest,
    RunRequestId,
)


class RefusalReason(StrEnum):
    EMPTY_SOURCE = "empty_source"
    SOURCE_TOO_LARGE = "source_too_large"
    STDIN_TOO_LARGE = "stdin_too_large"
    BOTH_INPUTS = "both_inputs"
    ALREADY_PENDING = "already_pending"
    TOO_SOON = "too_soon"


class RunRefused(Exception):
    """受け付けなかった。`message` は学習者にそのまま見せる文言。

    理由を分けて持つのは、画面が出し分けるため ── 「待っている実行がある」と
    「少し待って」では、学習者のすべきことが違う（受付の窓の #73 と同じ作法）。
    """

    def __init__(
        self, reason: RefusalReason, message: str, *, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        # 何秒後なら受け付けるか。`TOO_SOON` のときだけ。HTTP の Retry-After に使う。
        self.retry_after = retry_after


@dataclass(frozen=True)
class RunPolicy:
    """受付の条件。既定は設計書 §8.1 の値。"""

    max_source_bytes: int = MAX_SOURCE_BYTES
    max_stdin_bytes: int = MAX_STDIN_BYTES
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    stale_after_seconds: float = STALE_AFTER_SECONDS


@dataclass(frozen=True)
class RunView:
    """学習者が問い合わせたときに返すもの。"""

    request: RunRequest
    # 前に待っている要求の数。待っていなければ None。
    ahead: int | None


def request_run(
    queue: RunQueue,
    *,
    tenant_id: TenantId,
    learner_id: UserId,
    task_version_id: TaskVersionId,
    source: str,
    now: datetime,
    stdin: str = "",
    sample_name: str | None = None,
    ide_session_id: str | None = None,
    suffix: str | None = None,
    policy: RunPolicy | None = None,
) -> RunRequest:
    """実行要求を積む。受け付けなければ `RunRefused`。"""
    rules = policy or RunPolicy()

    if not source.strip():
        raise RunRefused(RefusalReason.EMPTY_SOURCE, "コードが空です。")
    if len(source.encode("utf-8")) > rules.max_source_bytes:
        raise RunRefused(
            RefusalReason.SOURCE_TOO_LARGE,
            f"コードが大きすぎます（{rules.max_source_bytes // 1024} KiB まで）。",
        )
    if len(stdin.encode("utf-8")) > rules.max_stdin_bytes:
        raise RunRefused(
            RefusalReason.STDIN_TOO_LARGE,
            f"標準入力が大きすぎます（{rules.max_stdin_bytes // 1024} KiB まで）。",
        )
    if sample_name is not None and stdin:
        raise RunRefused(
            RefusalReason.BOTH_INPUTS, "サンプルで実行するときは、標準入力は使えません。"
        )

    # **積む前に古い要求を片付ける。** runner が全部止まっていても、前の要求が
    # 30 秒で期限切れになり、次を積めるようにする（`RunQueue.expire_stale`）。
    queue.expire_stale(now, stale_after=rules.stale_after_seconds)

    if queue.in_flight_for(learner_id) is not None:
        raise RunRefused(RefusalReason.ALREADY_PENDING, "前の実行が終わるまでお待ちください。")
    latest = queue.latest_for(learner_id)
    if latest is not None:
        ready_at = latest.created_at + timedelta(seconds=rules.cooldown_seconds)
        if now < ready_at:
            wait = (ready_at - now).total_seconds()
            raise RunRefused(
                RefusalReason.TOO_SOON,
                f"続けて実行するには {rules.cooldown_seconds:g} 秒空けてください。",
                retry_after=wait,
            )

    request = RunRequest(
        id=RunRequestId(new_id("run")),
        tenant_id=tenant_id,
        learner_id=learner_id,
        task_version_id=task_version_id,
        ide_session_id=ide_session_id,
        suffix=suffix,
        source=source,
        stdin=stdin,
        sample_name=sample_name,
        created_at=now,
        updated_at=now,
    )
    try:
        queue.add(request)
    except RunAlreadyPending as exc:
        # 上の検査と積むあいだに、同じ学習者の別のタブが積んだ。SQL 実装では
        # 部分一意索引がここで止める。
        raise RunRefused(
            RefusalReason.ALREADY_PENDING, "前の実行が終わるまでお待ちください。"
        ) from exc
    return request


def view_run(
    queue: RunQueue,
    request_id: RunRequestId,
    *,
    learner_id: UserId,
    now: datetime,
    policy: RunPolicy | None = None,
) -> RunView | None:
    """学習者の問い合わせに答える。本人の要求でなければ None（404 にする）。

    **他人の要求は無いものとして扱う。** 「ある」と答えるだけで、ID を
    推測した誰かに他人が実行中であることが漏れる。
    """
    rules = policy or RunPolicy()
    # runner が止まっていても、画面が「待っています」のまま固まらないように、
    # 問い合わせのたびに古い要求を片付ける。
    queue.expire_stale(now, stale_after=rules.stale_after_seconds)
    request = queue.get(request_id)
    if request is None or request.learner_id != learner_id:
        return None
    return RunView(request=request, ahead=queue.position(request_id))
