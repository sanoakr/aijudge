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
        # 画像・PDF を選んだ／提出した（2026-09-25）。**ファイル名・大きさ・指紋だけ**で、
        # 中身は記録に入れない（画像は記録の容量と個人情報の両方で重い）。`submit` と
        # 別の種類にするのは、`submit` がエディタの内容の指紋との突き合わせ
        # （`integrity`・`flags.submission_mismatches`）に使われるため。
        "attach",
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


# 学習者をまたいで比べる貼り付けの最小の長さ（文字数・2026-09-25）。短い定型
# （`#include <stdio.h>` や 1 行の式）まで比べると、誰でも同じになって目印にならない。
# 画面が全文の写しを撮る「大きな貼り付け」（`BIG_PASTE_CHARS`）と揃えた目安。
SHARED_PASTE_CHARS = 80


class PasteMark(BaseModel):
    """外からの大きな貼り付け 1 回の**指紋だけ**（2026-09-25）。

    学習者をまたいで同じ内容の貼り付けを見つけるための索引。**中身は持たない**
    （中身は記録の本体にある）。記録を受け取るときに足し、記録と一緒に消す。
    主キー `(ide_session_id, seq, position)` が再送を重複させない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ide_session_id: IdeSessionId
    seq: int = Field(ge=0)
    # 束の中で何番目のイベントか。
    position: int = Field(ge=0)
    course_id: CourseId
    learner_id: UserId
    content_hash: str = Field(min_length=64, max_length=64)
    length: int = Field(ge=0)
    # 画面を開いてからの経過（ミリ秒）。
    t: float = Field(ge=0)


def paste_marks(session: IdeSession, seq: int, events: list[dict[str, Any]]) -> list[PasteMark]:
    """束の中の、外からの大きな貼り付けの指紋。エディタ内のコピーの貼り付けは入れない
    （問題文からのコピーもここに入る ── 全員が同じになるのは当然である）。"""
    marks: list[PasteMark] = []
    for position, event in enumerate(events):
        if event.get("type") != "paste" or event.get("origin") == "internal":
            continue
        digest = event.get("hash")
        length = event.get("len")
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        if not isinstance(length, int) or isinstance(length, bool) or length < SHARED_PASTE_CHARS:
            continue
        marks.append(
            PasteMark(
                ide_session_id=session.id,
                seq=seq,
                position=position,
                course_id=session.course_id,
                learner_id=session.learner_id,
                content_hash=digest,
                length=length,
                t=float(event.get("t", 0) or 0),
            )
        )
    return marks


class ActivityRejected(ValueError):
    """形が合わない。**受け取らない**（400）。記録の欠けとは別である。"""


@runtime_checkable
class ActivityIndex(Protocol):
    """行動記録の索引。インメモリと SQL の 2 実装に同じテストを当てる。"""

    def start_session(self, session: IdeSession) -> None: ...

    def get_session(self, session_id: IdeSessionId) -> IdeSession | None: ...

    def sessions_for(self, learner_id: UserId, course_id: CourseId) -> tuple[IdeSession, ...]:
        """この学習者のこのコースでのセッション。開いた順。教員の閲覧で使う。"""
        ...

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

    def course_sessions(self, course_id: CourseId) -> tuple[IdeSession, ...]:
        """このコースの全セッション。保存期間の purge が期限を調べる。"""
        ...

    def add_paste_marks(self, marks: Sequence[PasteMark]) -> None:
        """貼り付けの指紋を足す（2026-09-25）。**同じ鍵は足さない**（再送で重複しない）。"""
        ...

    def learners_sharing(
        self, course_id: CourseId, hashes: Sequence[str]
    ) -> dict[str, frozenset[UserId]]:
        """指紋 → そのコースで同じ内容を外から貼り付けた学習者。無い指紋は含めない。"""
        ...

    def delete_sessions(self, session_ids: Sequence[IdeSessionId]) -> int:
        """セッションとその索引を消す。消したセッションの数を返す。

        **本体（ファイル）を消してから呼ぶ**（`aijudge_admin.activity_purge`）。
        逆にすると、索引が無くなって本体を辿れないファイルが残る。
        """
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

    def read_batch(self, path: str) -> list[dict[str, Any]]:
        """索引の `path` からイベント列を読む。**根の外は読まない。**"""
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"{path!r} is outside the activity root")
        body = gzip.decompress(target.read_bytes()).decode("utf-8")
        return [json.loads(line) for line in body.splitlines() if line]

    def read_snapshot(self, session: IdeSession, name: str) -> str | None:
        """指紋から全文を読む。無ければ None（欠けた束に入っていた）。"""
        if len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
            return None
        target = self.session_dir(session) / "snapshots" / f"{name}.txt"
        return target.read_text(encoding="utf-8") if target.is_file() else None


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
