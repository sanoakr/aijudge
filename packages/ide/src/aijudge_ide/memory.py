"""インメモリの実行要求キュー。テストと、DB を持たない経路のため。

`packages/persistence` の実装と**同じテストに通る**ことが要件で、
両者が食い違ってよいのは並行性の扱いだけである。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from aijudge_core.ids import TaskId, UserId

from .buffer import IdeBuffer
from .protocols import RunAlreadyPending
from .run import DEFAULT_LEASE_SECONDS, RUNNER_LOST, RunRequest, RunRequestId, RunState


class InMemoryRunQueue:
    def __init__(self) -> None:
        self._requests: dict[RunRequestId, RunRequest] = {}

    def add(self, request: RunRequest) -> None:
        if request.id in self._requests:
            raise ValueError(f"run request {request.id} already exists")
        if request.state.in_flight and self.in_flight_for(request.learner_id) is not None:
            raise RunAlreadyPending(str(request.learner_id))
        self._requests[request.id] = request

    def reserve(
        self, now: datetime, *, worker: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> RunRequest | None:
        queued = self._ordered(r for r in self._requests.values() if r.state is RunState.QUEUED)
        if not queued:
            return None
        reserved = queued[0].reserved(now, worker=worker, lease_seconds=lease_seconds)
        self._requests[reserved.id] = reserved
        return reserved

    def update(self, request: RunRequest) -> None:
        if request.id not in self._requests:
            raise KeyError(request.id)
        self._requests[request.id] = request

    def get(self, request_id: RunRequestId) -> RunRequest | None:
        return self._requests.get(request_id)

    def in_flight_for(self, learner_id: UserId) -> RunRequest | None:
        return next(
            (
                r
                for r in self._requests.values()
                if r.learner_id == learner_id and r.state.in_flight
            ),
            None,
        )

    def latest_for(self, learner_id: UserId) -> RunRequest | None:
        mine = self._ordered(r for r in self._requests.values() if r.learner_id == learner_id)
        return mine[-1] if mine else None

    def position(self, request_id: RunRequestId) -> int | None:
        request = self._requests.get(request_id)
        if request is None or request.state is not RunState.QUEUED:
            return None
        key = (request.created_at, request.id)
        return sum(
            1
            for r in self._requests.values()
            if r.state is RunState.QUEUED and (r.created_at, r.id) < key
        )

    def expire_stale(self, now: datetime, *, stale_after: float) -> int:
        changed = 0
        for request in list(self._requests.values()):
            if request.is_stale(now, stale_after=stale_after):
                self._requests[request.id] = request.expired(now)
                changed += 1
            elif request.lease_lost(now):
                self._requests[request.id] = request.failed(now, RUNNER_LOST)
                changed += 1
        return changed

    def purge_finished(self, before: datetime) -> int:
        doomed = [
            r.id
            for r in self._requests.values()
            if r.terminal and r.finished_at is not None and r.finished_at < before
        ]
        for request_id in doomed:
            del self._requests[request_id]
        return len(doomed)

    @staticmethod
    def _ordered(requests: Iterable[RunRequest]) -> list[RunRequest]:
        return sorted(requests, key=lambda r: (r.created_at, r.id))


class InMemoryBufferStore:
    """インメモリの自動保存。SQL 実装と同じテストに通る。"""

    def __init__(self) -> None:
        self._buffers: dict[tuple[UserId, TaskId], IdeBuffer] = {}

    def save(self, buffer: IdeBuffer) -> None:
        self._buffers[(buffer.learner_id, buffer.task_id)] = buffer

    def get(self, learner_id: UserId, task_id: TaskId) -> IdeBuffer | None:
        return self._buffers.get((learner_id, task_id))

    def for_tasks(self, learner_id: UserId, task_ids: list[TaskId]) -> dict[TaskId, IdeBuffer]:
        return {
            task_id: buffer
            for task_id in task_ids
            if (buffer := self._buffers.get((learner_id, task_id))) is not None
        }
