"""行動記録（ADR 0023・設計書 §6）。4 つ目の記録であり、何も止めない。

学習者がコードを**どう書いたか**を、時刻付きのイベント列とコード全文の
スナップショットで残す。生成 AI や他の学習者からの貼り付けを**推定する材料**に
するためで、判定には使わない。成績には一切つながない（`grading-does-not-know-ide`）。

置き場は 2 つに分ける（ADR 0023 §3）。

- **本体はファイル**（`AIJUDGE_ACTIVITY_DIR`、動画と同じストレージ）:
  `{course}/{unit}/{learner}/{ide_session}/events/{seq}.ndjson.gz` と
  `.../snapshots/{sha256}.txt`
- **索引は DB**（`ide_sessions`・`ide_event_batches`）。バッチに
  `(ide_session_id, seq)` の一意制約を掛け、再送を重複させない

**書く順はファイル → DB。** 一時ファイルに書いて `rename` するので、途中で
落ちても残るのは孤立したファイルだけで、半端な索引は残らない。再送された同じ
seq は同じ場所に同じ中身で上書きされる。

**受け口では解析しない**（ADR 0023「やらないこと」）。形を確かめて書くだけ。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NewType, Protocol, runtime_checkable
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import CourseId, TenantId, UserId

from .run import MAX_SOURCE_BYTES

IdeSessionId = NewType("IdeSessionId", str)

# 送ってよいイベントの種類（設計書 §6.2）。**知らない種類は受け取らない** ──
# 何でも受けると、何を記録しているかを告知の文面で約束できなくなる。
EVENT_TYPES = frozenset(
    {
        "hello",
        "heartbeat",
        "edit",
        "paste",
        "copy",
        "cut",
        "suggest",
        "file_load",
        "run",
        "submit",
        "tab",
        "focus",
        "blur",
        "visibility",
        "format",
        # 束の末尾に付ける各タブの内容の指紋（設計書 §6.4 の突き合わせ用）。
        "tabs",
    }
)

# 1 バッチの上限。平常は 10 秒分で数百件・数 KB。**貼り付け 1 回が 64 KiB まで**
# あるので、件数ではなくバイト数で止める。
MAX_EVENTS_PER_BATCH = 5000
MAX_BATCH_BYTES = 512 * 1024
MAX_SNAPSHOTS_PER_BATCH = 16
# 利用者エージェントは長さを切る。本文を入れる場所ではない。
MAX_USER_AGENT = 300


class IdeSession(BaseModel):
    """IDE の画面を 1 回開いてから閉じるまで。行動記録はこの単位で束ねる。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: IdeSessionId
    tenant_id: TenantId
    learner_id: UserId
    course_id: CourseId
    unit: str | None = None
    started_at: datetime
    user_agent: str = Field(default="", max_length=MAX_USER_AGENT)
    # **告知を読んで確認した時刻**（ADR 0023 §5）。確認なしのセッションは作らない。
    consented_at: datetime


class EventBatch(BaseModel):
    """1 回の送信で届いたイベントの束の索引。本体はファイルにある。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ide_session_id: IdeSessionId
    seq: int = Field(ge=0)
    received_at: datetime
    # 学習者の PC の壁時計（送信時）。サーバの受信時刻との差から、PC の時計の
    # ずれを後で推定する（設計書 §6.2）。信じるものではなく、比べるもの。
    client_time: datetime | None = None
    event_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    # イベント列の本体（圧縮前の NDJSON）の SHA-256。ファイルが後から書き換え
    # られていないかを確かめるため。
    sha256: str = Field(min_length=64, max_length=64)
    # 活動記録の根からの相対パス。
    path: str = Field(min_length=1, max_length=512)


class ActivityRejected(ValueError):
    """形が合わない。**受け取らない**（400）。記録の欠けとは別である。"""


@runtime_checkable
class ActivityIndex(Protocol):
    """行動記録の索引。インメモリと SQL の 2 実装に同じテストを当てる。"""

    def start_session(self, session: IdeSession) -> None: ...

    def get_session(self, session_id: IdeSessionId) -> IdeSession | None: ...

    def has_consented(self, learner_id: UserId, course_id: CourseId) -> bool:
        """このコースで告知を確認したことがあるか。あれば画面は告知を出し直さない。"""
        ...

    def add_batch(self, batch: EventBatch) -> bool:
        """索引を 1 行足す。**同じ (セッション, seq) がすでにあれば何もせず False**。

        再送は日常で（通信の不調、`pagehide` の送り直し）、重複させない。
        """
        ...

    def batches(self, session_id: IdeSessionId) -> tuple[EventBatch, ...]:
        """seq の順。欠落（seq の飛び）は呼び出し側が数える。"""
        ...

    def delete_for_course(self, course_id: CourseId) -> tuple[IdeSession, ...]:
        """このコースのセッションと索引を消し、消したセッションを返す。

        **コースを消すときにだけ使う**（`aijudge_admin.courses`）。返したセッションの
        本体（ファイル）は呼び出し側が消す ── 索引だけ消してファイルを残すと、
        学習者の記録が誰にも辿れないまま残る。
        """
        ...


def check_events(events: object) -> list[dict[str, Any]]:
    """イベント列の形だけを確かめる。**中身の解釈はしない**（受信時に解析しない）。"""
    if not isinstance(events, list):
        raise ActivityRejected("events must be a list")
    if len(events) > MAX_EVENTS_PER_BATCH:
        raise ActivityRejected(f"too many events (max {MAX_EVENTS_PER_BATCH})")
    checked: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            raise ActivityRejected("each event must be an object")
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            raise ActivityRejected(f"unknown event type: {kind!r}")
        t = event.get("t")
        if not isinstance(t, int | float) or isinstance(t, bool) or t < 0:
            raise ActivityRejected("each event needs a non-negative time 't'")
        checked.append(event)
    return checked


def snapshot_name(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_snapshots(snapshots: object) -> dict[str, str]:
    """スナップショットは `{sha256: 全文}`。**鍵が中身の指紋と一致すること。**

    一致しないものを受けると、後で「このハッシュの内容」を引いたときに別の
    内容が出てくる。記録の信頼性そのものなので、ここは断る。
    """
    if snapshots is None:
        return {}
    if not isinstance(snapshots, dict):
        raise ActivityRejected("snapshots must be an object")
    if len(snapshots) > MAX_SNAPSHOTS_PER_BATCH:
        raise ActivityRejected(f"too many snapshots (max {MAX_SNAPSHOTS_PER_BATCH})")
    for name, text in snapshots.items():
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ActivityRejected("a snapshot must be text within the source limit")
        if snapshot_name(text) != name:
            raise ActivityRejected("a snapshot's name must be the SHA-256 of its text")
    return dict(snapshots)


class ActivityFiles:
    """行動記録の本体をファイルに書く。

    **動画と同じ根の下に置く**（ADR 0023 §3）。保存期間の purge は問題セットの
    単位でディレクトリごと消す（段階 4）ので、階層を
    `{course}/{unit}/{learner}/{ide_session}` にする。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def session_dir(self, session: IdeSession) -> Path:
        # 問題セットの名前は日本語や `/` を含みうる。**経路を抜け出させない**よう
        # 1 つの部品に符号化する（空は `_`）。
        unit = quote(session.unit, safe="") if session.unit else "_"
        return self.root / str(session.course_id) / unit / str(session.learner_id) / str(session.id)

    def write_batch(
        self,
        session: IdeSession,
        seq: int,
        events: Sequence[dict[str, Any]],
        snapshots: dict[str, str],
    ) -> tuple[str, str, int]:
        """イベント列とスナップショットを書く。(相対パス, SHA-256, バイト数) を返す。"""
        directory = self.session_dir(session)
        body = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events
        ).encode("utf-8")
        target = directory / "events" / f"{seq:08d}.ndjson.gz"
        _atomic_write(target, gzip.compress(body, mtime=0))
        for name, text in snapshots.items():
            snapshot = directory / "snapshots" / f"{name}.txt"
            # 中身の指紋で名付けているので、同じ名前なら同じ中身。書き直さない。
            if not snapshot.exists():
                _atomic_write(snapshot, text.encode("utf-8"))
        return (
            str(target.relative_to(self.root)),
            hashlib.sha256(body).hexdigest(),
            len(body),
        )


def _atomic_write(target: Path, payload: bytes) -> None:
    """一時ファイルに書いて `rename` する。途中で落ちても半端なファイルを残さない。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
