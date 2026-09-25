"""インメモリの実行要求キュー。テストと、DB を持たない経路のため。

`packages/persistence` の実装と**同じテストに通る**ことが要件で、
両者が食い違ってよいのは並行性の扱いだけである。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from aijudge_core.ids import CourseId, SubmissionId, TaskId, UserId

from .activity import EventBatch, IdeSession, IdeSessionId, PasteMark
from .buffer import IdeBuffer
from .links import SubmissionLink
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

    def for_task(self, task_id: TaskId) -> tuple[IdeBuffer, ...]:
        return tuple(b for (_, t), b in sorted(self._buffers.items()) if t == task_id)

    def task_ids(self) -> tuple[TaskId, ...]:
        return tuple(sorted({task_id for (_, task_id) in self._buffers}))

    def delete(self, learner_id: UserId, task_id: TaskId) -> None:
        self._buffers.pop((learner_id, task_id), None)


class InMemorySubmissionLinkStore:
    """インメモリの出どころの記録。SQL 実装と同じテストに通る。"""

    def __init__(self) -> None:
        self._links: dict[SubmissionId, SubmissionLink] = {}

    def record(self, link: SubmissionLink) -> None:
        self._links.setdefault(link.submission_id, link)

    def for_submission(self, submission_id: SubmissionId) -> SubmissionLink | None:
        return self._links.get(submission_id)


class InMemoryActivityIndex:
    """インメモリの行動記録の索引。SQL 実装と同じテストに通る。"""

    def __init__(self) -> None:
        self._sessions: dict[IdeSessionId, IdeSession] = {}
        self._batches: dict[tuple[IdeSessionId, int], EventBatch] = {}
        self._pastes: dict[tuple[IdeSessionId, int, int], PasteMark] = {}

    def start_session(self, session: IdeSession) -> None:
        if session.id in self._sessions:
            raise ValueError(f"ide session {session.id} already exists")
        self._sessions[session.id] = session

    def get_session(self, session_id: IdeSessionId) -> IdeSession | None:
        return self._sessions.get(session_id)

    def sessions_for(self, learner_id: UserId, course_id: CourseId) -> tuple[IdeSession, ...]:
        return tuple(
            sorted(
                (
                    s
                    for s in self._sessions.values()
                    if s.learner_id == learner_id and s.course_id == course_id
                ),
                key=lambda s: (s.started_at, s.id),
            )
        )

    def has_consented(self, learner_id: UserId, course_id: CourseId) -> bool:
        return any(
            s.learner_id == learner_id and s.course_id == course_id for s in self._sessions.values()
        )

    def add_batch(self, batch: EventBatch) -> bool:
        key = (batch.ide_session_id, batch.seq)
        if key in self._batches:
            return False
        self._batches[key] = batch
        return True

    def batches(self, session_id: IdeSessionId) -> tuple[EventBatch, ...]:
        return tuple(
            batch for (sid, _), batch in sorted(self._batches.items()) if sid == session_id
        )

    def course_sessions(self, course_id: CourseId) -> tuple[IdeSession, ...]:
        return tuple(
            sorted(
                (s for s in self._sessions.values() if s.course_id == course_id),
                key=lambda s: (s.started_at, s.id),
            )
        )

    def add_paste_marks(self, marks: Sequence[PasteMark]) -> None:
        for mark in marks:
            self._pastes.setdefault((mark.ide_session_id, mark.seq, mark.position), mark)

    def learners_sharing(
        self, course_id: CourseId, hashes: Sequence[str]
    ) -> dict[str, frozenset[UserId]]:
        wanted = set(hashes)
        found: dict[str, set[UserId]] = {}
        for mark in self._pastes.values():
            if mark.course_id == course_id and mark.content_hash in wanted:
                found.setdefault(mark.content_hash, set()).add(mark.learner_id)
        return {digest: frozenset(learners) for digest, learners in found.items()}

    def _drop_pastes(self, session_id: IdeSessionId) -> None:
        for key in [k for k in self._pastes if k[0] == session_id]:
            del self._pastes[key]

    def delete_sessions(self, session_ids: Sequence[IdeSessionId]) -> int:
        removed = 0
        for session_id in session_ids:
            if self._sessions.pop(session_id, None) is not None:
                removed += 1
            for key in [k for k in self._batches if k[0] == session_id]:
                del self._batches[key]
            # 貼り付けの指紋も記録と一緒に消す（残すと、消した記録の痕跡が残る）。
            self._drop_pastes(session_id)
        return removed

    def delete_for_course(self, course_id: CourseId) -> tuple[IdeSession, ...]:
        doomed = tuple(s for s in self._sessions.values() if s.course_id == course_id)
        for session in doomed:
            del self._sessions[session.id]
            for key in [k for k in self._batches if k[0] == session.id]:
                del self._batches[key]
            self._drop_pastes(session.id)
        return doomed
