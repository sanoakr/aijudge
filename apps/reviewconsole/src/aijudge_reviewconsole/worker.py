"""採点ワーカー — 提出を採点する。レビューとは独立に走る。

**採点はレビューの前で走る。** 以前はレビューコンソールが blind 採点の保存後に
採点を起動していたため、測定用データの入力が採点の前提条件になっていた。
これは手段と目的が逆で、測定を必須機能にしないという方針に反する（ADR 0007）。

Phase 0 では S3（Submission & Orchestration）が未実装なので、ここが
その最小の代替である。本来はキューに載ったジョブを消費する常駐ワーカーで、
「提出 → 採点 → あとから結果が届く」という非同期の経路になる。
ここでは提出物ディレクトリを走査して、採点が無いものを採点する。

観測レコードの書き出しもここで行うが、**失敗しても採点は成立させる**
（S6 停止時と同じ劣化動作の扱い、P2）。測定は採点の必須機能ではない。
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    GradingRun,
    Submission,
    SubmissionState,
    new_id,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_grading import EvaluatorRegistry, GradingPipeline, load_profile

from .projection import project
from .store import QueueEntry, ReviewStore
from .tasks import TaskLoader

# 提出物の拡張子 → Artifact の種別。
_KINDS: dict[str, ArtifactKind] = {
    ".c": ArtifactKind.CODE,
    ".py": ArtifactKind.CODE,
    ".java": ArtifactKind.CODE,
    ".tex": ArtifactKind.LATEX,
    ".md": ArtifactKind.MARKDOWN,
}


def _submission_for(entry: QueueEntry) -> tuple[Submission, bytes]:
    payload = entry.source_path.read_bytes()
    submission_id = SubmissionId(new_id("sub"))
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=_KINDS.get(entry.source_path.suffix.lower(), ArtifactKind.MARKDOWN),
        filename=entry.submission,
        storage_key=f"file://{entry.source_path}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=datetime.now(UTC),
    )
    return (
        Submission(
            id=submission_id,
            task_version_id=TaskVersionId(new_id("tsv")),
            learner_id=UserId(new_id("usr")),
            state=SubmissionState.SUBMITTED,
            artifacts=(artifact,),
            created_at=datetime.now(UTC),
            submitted_at=datetime.now(UTC),
        ),
        payload,
    )


class Grader:
    """提出を採点し、結果と観測を保存する。"""

    def __init__(
        self,
        store: ReviewStore,
        profiles_dir: Path,
        tasks: TaskLoader,
        *,
        registry: EvaluatorRegistry | None = None,
    ) -> None:
        self.store = store
        self.profiles_dir = profiles_dir
        self.tasks = tasks
        self.registry = registry or EvaluatorRegistry().load_installed()

    def grade(self, entry: QueueEntry) -> GradingRun:
        """1 件採点して保存する。既に採点済みでも引き直す（呼び出し側が判断する）。"""
        task = self.tasks.task_for(entry)
        profile = load_profile(self.profiles_dir / f"{entry.subject_profile}.yaml", self.registry)
        submission, payload = _submission_for(entry)
        run = GradingPipeline(self.registry, profile).run(task, submission, lambda _: payload)
        self.store.save_run(entry, run)
        self.record_observations(entry, run)
        return run

    def record_observations(self, entry: QueueEntry, run: GradingRun) -> None:
        """観測を書き出す。**失敗しても採点は成立させる。**

        測定は採点の必須機能ではない（ADR 0007）。ここで例外を投げると
        測定の都合で採点が落ちることになり、方針が逆転する。
        """
        with contextlib.suppress(Exception):
            self.store.save_observations(
                entry,
                project(
                    entry,
                    self.tasks.task_for(entry),
                    run,
                    mark=self.store.load_blind_mark(entry),
                    decision=self.store.load_decision(entry),
                ),
            )


def grade_pending(
    grader: Grader,
    *,
    subject_profile: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[int, tuple[str, ...]]:
    """採点が無い提出をすべて採点する。

    冪等。既に `runs/` がある提出は飛ばす。1 件の失敗で全体を止めない
    （締切前に 1 件の異常提出で全員の採点が止まるのは受け入れられない）。
    """
    graded = 0
    errors: list[str] = []
    pending = [entry for entry in grader.store.queue(subject_profile) if not entry.graded]

    for index, entry in enumerate(pending, 1):
        if progress:
            progress(f"[{index}/{len(pending)}] {entry.id} — 採点中")
        try:
            grader.grade(entry)
        except Exception as exc:  # 1 件の失敗で全体を止めない
            errors.append(f"{entry.id}: {type(exc).__name__}: {exc}")
            continue
        graded += 1

    return graded, tuple(errors)
