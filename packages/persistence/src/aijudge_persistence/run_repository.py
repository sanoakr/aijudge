"""試しの実行の要求キューの SQLAlchemy 実装（ADR 0024）。

インメモリ実装（`aijudge_ide.InMemoryRunQueue`）と**同じ規則**を守る。
`packages/persistence/tests/test_run_repository.py` が同じテストを両方に
通す。食い違ってよいのは行ロックだけで、そこは明示的に分岐する。

どのメソッドも commit しない。呼び出し側の UnitOfWork が commit する。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aijudge_core.ids import UserId
from aijudge_ide import (
    DEFAULT_LEASE_SECONDS,
    IN_FLIGHT_STATES,
    RUNNER_LOST,
    TERMINAL_STATES,
    RunAlreadyPending,
    RunRequest,
    RunRequestId,
    RunState,
)

from .schema import RunRequestRow


def _row(request: RunRequest) -> RunRequestRow:
    row = RunRequestRow(id=str(request.id))
    _apply(row, request)
    return row


def _apply(row: RunRequestRow, request: RunRequest) -> None:
    row.tenant_id = str(request.tenant_id)
    row.learner_id = str(request.learner_id)
    row.task_version_id = str(request.task_version_id)
    row.state = request.state.value
    row.created_at = request.created_at
    row.finished_at = request.finished_at
    row.lease_expires_at = request.lease_expires_at
    row.document = request.model_dump(mode="json")


def _request(row: RunRequestRow) -> RunRequest:
    return RunRequest.model_validate(row.document)


_IN_FLIGHT = tuple(state.value for state in IN_FLIGHT_STATES)
_TERMINAL = tuple(state.value for state in TERMINAL_STATES)


class SqlRunQueue:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def _locks_rows(self) -> bool:
        # SQLite は行ロックを持たない。runner は 1 本だけにする前提で許容する
        # （`supports_row_locking` と同じ判断）。
        return self._session.bind is not None and self._session.bind.dialect.name != "sqlite"

    def add(self, request: RunRequest) -> None:
        self._session.add(_row(request))
        try:
            self._session.flush()
        except IntegrityError as exc:
            # **部分一意索引が止めた**（`uq_run_requests_one_in_flight`）。同じ
            # 学習者が別のタブから同時に押した。
            #
            # セッションごと巻き戻る ── 同じトランザクションで先に済ませた
            # 期限切れの片付けも消えるが、次の要求がもう一度片付けるので害は
            # 無い。受付の UnitOfWork はこの要求を積むためだけに開いている。
            self._session.rollback()
            raise RunAlreadyPending(str(request.learner_id)) from exc

    def reserve(
        self, now: datetime, *, worker: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> RunRequest | None:
        """待っている要求を古い順に 1 件取る。

        **行ロックを取る**（`with_for_update(skip_locked=True)`）。取らないと、
        2 つの runner が同じ要求を読んで両方が実行する。`skip_locked` なのは、
        他の runner が見ている行を待たずに次へ行くため（採点キューと同じ型）。
        """
        statement = (
            select(RunRequestRow)
            .where(RunRequestRow.state == RunState.QUEUED.value)
            .order_by(RunRequestRow.created_at, RunRequestRow.id)
            .limit(1)
        )
        if self._locks_rows:
            statement = statement.with_for_update(skip_locked=True)
        row = self._session.execute(statement).scalars().first()
        if row is None:
            return None
        reserved = _request(row).reserved(now, worker=worker, lease_seconds=lease_seconds)
        _apply(row, reserved)
        self._session.flush()
        return reserved

    def update(self, request: RunRequest) -> None:
        row = self._session.get(RunRequestRow, str(request.id))
        if row is None:
            raise KeyError(request.id)
        _apply(row, request)
        self._session.flush()

    def get(self, request_id: RunRequestId) -> RunRequest | None:
        row = self._session.get(RunRequestRow, str(request_id))
        return None if row is None else _request(row)

    def in_flight_for(self, learner_id: UserId) -> RunRequest | None:
        row = (
            self._session.execute(
                select(RunRequestRow).where(
                    RunRequestRow.learner_id == str(learner_id),
                    RunRequestRow.state.in_(_IN_FLIGHT),
                )
            )
            .scalars()
            .first()
        )
        return None if row is None else _request(row)

    def latest_for(self, learner_id: UserId) -> RunRequest | None:
        row = (
            self._session.execute(
                select(RunRequestRow)
                .where(RunRequestRow.learner_id == str(learner_id))
                .order_by(RunRequestRow.created_at.desc(), RunRequestRow.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        return None if row is None else _request(row)

    def position(self, request_id: RunRequestId) -> int | None:
        row = self._session.get(RunRequestRow, str(request_id))
        if row is None or row.state != RunState.QUEUED.value:
            return None
        # `reserve` と同じ順序（created_at, id）で、自分より前を数える。
        ahead = self._session.execute(
            select(func.count())
            .select_from(RunRequestRow)
            .where(
                RunRequestRow.state == RunState.QUEUED.value,
                or_(
                    RunRequestRow.created_at < row.created_at,
                    and_(
                        RunRequestRow.created_at == row.created_at,
                        RunRequestRow.id < row.id,
                    ),
                ),
            )
        ).scalar_one()
        return int(ahead)

    def expire_stale(self, now: datetime, *, stale_after: float) -> int:
        """古い要求を片付ける。

        `document` も書き換えるので、UPDATE 1 文ではなく行を読んで遷移させる。
        対象は「30 秒以上待った要求」と「リースの切れた要求」だけで、平常時は
        0 件である（索引 `ix_run_requests_state_created` で引く）。

        行ロックは `skip_locked` で取る。runner が取ろうとしている行や、別の
        受付が片付けている行を待たない ── どちらが片付けても結果は同じ。
        """
        stale_before = now - timedelta(seconds=stale_after)
        statement = select(RunRequestRow).where(
            or_(
                and_(
                    RunRequestRow.state == RunState.QUEUED.value,
                    RunRequestRow.created_at <= stale_before,
                ),
                and_(
                    RunRequestRow.state == RunState.RUNNING.value,
                    RunRequestRow.lease_expires_at.is_not(None),
                    RunRequestRow.lease_expires_at <= now,
                ),
            )
        )
        if self._locks_rows:
            statement = statement.with_for_update(skip_locked=True)
        changed = 0
        for row in self._session.execute(statement).scalars().all():
            request = _request(row)
            if request.state is RunState.QUEUED:
                _apply(row, request.expired(now))
            else:
                _apply(row, request.failed(now, RUNNER_LOST))
            changed += 1
        if changed:
            self._session.flush()
        return changed

    def purge_finished(self, before: datetime) -> int:
        result = self._session.execute(
            delete(RunRequestRow).where(
                RunRequestRow.state.in_(_TERMINAL),
                RunRequestRow.finished_at.is_not(None),
                RunRequestRow.finished_at < before,
            )
        )
        self._session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
