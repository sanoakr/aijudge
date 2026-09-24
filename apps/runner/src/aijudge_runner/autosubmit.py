"""受付終了時の自動提出（設計書 §9.1・2026-09-24 決定）。

**受付の終わり（`accepts_until`）に、サーバが自動保存の最新を提出する。**
対象は、一度も提出していない課題と、最後の提出のあとに書き換えた課題。

- ブラウザ主導にしない。通信が切れた・PC がスリープした学習者の分が漏れる
- **成績は最高得点の提出が採用される**（`progress.py`）ので、本人が出すつもりの
  なかった版が出ても、成績を下げることはない
- 既存の提出の経路（`SubmissionService.accept`）を通す。採点側は区別しない（I2）。
  区別は提出の外の記録（`ide_submission_links` の `auto_close`）で残す

**何度走らせても同じ結果になる。** 提出は内容で重複を畳む（`accept` の冪等
キー）ので、同じ自動保存を 2 度出しても 2 件目はできない。すでに同じ内容を
本人が出していれば、それも新しい提出にならない。

**提出時刻は自動保存の時刻にする。** 走った時刻にすると、受付終了の数分後の
提出として扱われ、締切と受付終了のあいだの課題では書いた時点より遅い提出と
して減点される。内容がその時刻に存在したことは自動保存が示している。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aijudge_core import AnswerMode, Course, Role, Task, TaskVersion, allowed_suffixes
from aijudge_ide import (
    EditorFormat,
    IdeBuffer,
    SubmissionLink,
    SubmissionOrigin,
    editor_formats,
)
from aijudge_identity import demo_course_from_env
from aijudge_persistence import Database
from aijudge_submission import ArtifactStore, IncomingFile, SubmissionRejected, SubmissionService

logger = logging.getLogger(__name__)

# 受付終了からどれだけ遡って見るか（時間）。これより前に閉じた課題は、すでに
# 前の回で処理してある。**冪等なので重なっても害は無い** ── 間隔（1 分）より
# ずっと長くとり、自動提出のプロセスが半日止まっていても取りこぼさない。
DEFAULT_LOOKBACK_HOURS = 24.0


@dataclass
class CloseReport:
    """1 回の自動提出の結果。**出さなかった件数と理由も残す**（finalize と同じ作法）。"""

    submitted: int = 0
    unchanged: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def close_editor_tasks(
    database: Database,
    store: ArtifactStore,
    *,
    now: datetime,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    dry_run: bool = False,
) -> CloseReport:
    """受付を終えたエディタの課題について、自動保存の最新を提出する。"""
    report = CloseReport()
    since = now - timedelta(hours=lookback_hours)

    with database.unit_of_work() as uow:
        candidates: list[tuple[Task, TaskVersion, Course, tuple[IdeBuffer, ...]]] = []
        for task_id in uow.ide_buffers.task_ids():
            task = uow.tasks.get_task(task_id)
            if task is None or not _closing(task, since=since, now=now):
                continue
            version = uow.tasks.latest_published_version(task.id)
            course = uow.identity.get_course(task.course_id)
            if version is None or course is None:
                continue
            candidates.append((task, version, course, uow.ide_buffers.for_task(task.id)))

    demo = demo_course_from_env()
    for task, version, course, buffers in candidates:
        accepted = allowed_suffixes(task.accepted_suffixes, course.upload_suffixes)
        formats = {f.suffix: f for f in editor_formats(accepted)}
        for buffer in buffers:
            _close_one(
                database,
                store,
                task=task,
                version=version,
                course=course,
                buffer=buffer,
                formats=formats,
                is_demo=demo is not None and str(demo.course_id) == str(course.id),
                now=now,
                dry_run=dry_run,
                report=report,
            )
    return report


def _closing(task: Task, *, since: datetime, now: datetime) -> bool:
    """いま自動提出すべき課題か。"""
    return (
        not task.withdrawn
        and task.answer_mode is AnswerMode.EDITOR
        and task.accepts_until is not None
        and since < task.accepts_until <= now
    )


def _close_one(
    database: Database,
    store: ArtifactStore,
    *,
    task: Task,
    version: TaskVersion,
    course: Course,
    buffer: IdeBuffer,
    formats: dict[str, EditorFormat],
    is_demo: bool,
    now: datetime,
    dry_run: bool,
    report: CloseReport,
) -> None:
    assert task.accepts_until is not None
    if not buffer.source.strip():
        report.skip("空")
        return
    # 受付を過ぎてから届いた自動保存は採らない（受け口がすでに断っているが、
    # ここでも確かめる ── 時計の境目で 1 件すべり込んでも出さない）。
    if buffer.updated_at > task.accepts_until:
        report.skip("受付終了後の保存")
        return
    chosen = formats.get(buffer.suffix)
    if chosen is None:
        # 教員が保存のあとで提出形式を狭めた。出しても受付と食い違う。
        report.skip("課題が受け付けない形式")
        return
    with database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(task.course_id, buffer.learner_id)
    # **学習者の分だけ出す。** 教員・TA の書きかけは動作確認で、自動で提出に
    # すると採点一覧に試行が並ぶ。受講をやめた人の分も出さない。
    if enrollment is None or enrollment.role is not Role.LEARNER:
        report.skip("学習者ではない")
        return
    if dry_run:
        report.submitted += 1
        return

    saved_at = buffer.updated_at
    clock: Callable[[], datetime] = lambda: saved_at  # noqa: E731
    service = SubmissionService(database.unit_of_work, store, clock=clock)
    try:
        result = service.accept(
            tenant_id=buffer.tenant_id,
            task_version_id=version.id,
            learner_id=buffer.learner_id,
            subject_profile=version.subject_profile,
            files=[
                IncomingFile(
                    filename=chosen.filename,
                    kind=chosen.kind,
                    payload=buffer.source.encode("utf-8"),
                )
            ],
            grading_starts_at=task.grading_starts_at,
            submitted_as=Role.LEARNER,
            is_demo=is_demo,
        )
    except SubmissionRejected as exc:
        report.failures.append(f"{task.title} / {buffer.learner_id}: {exc}")
        logger.warning("auto-submit was rejected", extra={"task_id": str(task.id)})
        return

    if result.deduplicated:
        # 同じ内容はもう出ている（本人が押したか、前の回で出した）。**出どころの
        # 記録も書かない** ── 本人がファイルで出した提出に「自動」の印を付けない。
        report.unchanged += 1
        return
    with database.unit_of_work() as uow:
        uow.ide_links.record(
            SubmissionLink(
                submission_id=result.submission.id,
                tenant_id=buffer.tenant_id,
                learner_id=buffer.learner_id,
                task_id=task.id,
                origin=SubmissionOrigin.AUTO_CLOSE,
                content_hash=buffer.content_hash,
                recorded_at=now,
            )
        )
        uow.commit()
    report.submitted += 1
