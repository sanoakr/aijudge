"""実行要求のキューの口。

実装は 2 つある ── インメモリ（`memory.py`）と PostgreSQL/SQLite
（`packages/persistence`）── で、`packages/persistence/tests` が**同じテストを
両方に通す**。この repo の他の Store / Repository と同じ約束である。

**採点キュー（`grading_jobs`）とは別の表・別の口である**（ADR 0024 §1）。
同じ表に相乗りすると、試験中の実行の山が採点を待たせ、採点の山が学習者の
画面を固める。どのメソッドも commit しない ── 呼び出し側の UnitOfWork が
commit する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from aijudge_core.ids import UserId

from .run import DEFAULT_LEASE_SECONDS, RunRequest, RunRequestId


class RunAlreadyPending(Exception):
    """この学習者にはまだ終わっていない要求がある。

    **1 人が同時に待てるのは 1 件**（ADR 0024 §1）。インメモリ実装は自分で
    確かめ、SQL 実装は部分一意索引が止める ── 画面でボタンを押せなくする
    だけでは境界にならない（#146 と同じ理由）。
    """


@runtime_checkable
class RunQueue(Protocol):
    def add(self, request: RunRequest) -> None:
        """積む。同じ学習者の未了の要求があれば `RunAlreadyPending`。"""
        ...

    def reserve(
        self, now: datetime, *, worker: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> RunRequest | None:
        """待っている要求を古い順に 1 件取る。無ければ None。

        **古くなった要求は取らない前提である** ── 呼ぶ前に `expire_stale` を
        呼ぶこと（runner はそうしている）。ここで黙って飛ばすと、飛ばした行が
        いつまでも QUEUED で残り、その学習者は次を積めない。
        """
        ...

    def update(self, request: RunRequest) -> None: ...

    def get(self, request_id: RunRequestId) -> RunRequest | None: ...

    def in_flight_for(self, learner_id: UserId) -> RunRequest | None:
        """この学習者のまだ終わっていない要求。"""
        ...

    def latest_for(self, learner_id: UserId) -> RunRequest | None:
        """この学習者が最後に積んだ要求（状態を問わない）。連続実行の間隔を見る。"""
        ...

    def position(self, request_id: RunRequestId) -> int | None:
        """この要求の前に待っている要求の数。待っていなければ None。

        画面の「あと N 件」。`reserve` と同じ順序（`created_at, id`）で数える。
        """
        ...

    def expire_stale(self, now: datetime, *, stale_after: float) -> int:
        """古くなった要求を片付ける。片付けた件数を返す。

        - 待ったまま `stale_after` 秒を過ぎた要求 → EXPIRED（実行しない）
        - runner が握ったままリースが切れた要求 → FAILED

        **runner だけが呼ぶのではない。** 受付（web）も積む前に呼ぶ。runner が
        全部止まっていても、学習者の要求が永久に QUEUED で残って次を積めなく
        なることがない（ADR 0024 の帰結「30 秒で期限切れになる」）。
        """
        ...

    def purge_finished(self, before: datetime) -> int:
        """`before` より前に終わった要求を消す。消した件数を返す。

        **結果は残さない**（ADR 0024 §1）。行には学習者のコードそのものが
        入っているので、画面に出したあとまで持ち続ける理由が無い。
        """
        ...
