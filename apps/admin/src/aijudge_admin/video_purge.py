"""保存期間を過ぎた動画を消す（ADR 0020）。

**起点は課題の締切で、6 ヶ月。** 期限の計算はコアにある
（`aijudge_core.video_retention_expires_at`）。ここが持つのは「どれが対象か」
を集める手順と、ファイルを消して印を付ける順序である。

**2 段になっているのは、途中で落ちても続けられるようにするため。**
先にファイルを消し、消せたものにだけ印を付ける。逆にすると、印が付いたのに
ファイルが残った状態を次の実行が見つけられない（印の付いたものは対象から
外れる）。ファイルだけ先に消えた状態は、次の実行がもう一度拾って印を付ける
── ストアの `delete` は無いキーを成功として扱う。

**自動では走らせない。** 呼ぶのは `aijudge-admin video purge` だけで、既定は
下見（`--dry-run`）である。提出を消す操作を定期実行する仕掛けを作ると、
それが本物のコースに向く事故の余地が残る（`demo reset` と同じ判断・#194）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import ArtifactKind, Course, video_retention_expires_at
from aijudge_core.ids import ArtifactId, CourseId, SubmissionId, TenantId, UserId
from aijudge_persistence import Database
from aijudge_submission import StreamingArtifactStore

from .operations import AdminError

__all__ = ["PurgeCandidate", "PurgeOutcome", "PurgePlan", "plan_video_purge", "purge_videos"]


@dataclass(frozen=True)
class PurgeCandidate:
    """消してよい動画 1 件。"""

    course_code: str
    #: 問題セットの名前（`ex03`）。無ければ `None`。**まとまりの単位**である。
    unit: str | None
    task_title: str
    due_at: datetime
    expires_at: datetime
    submission_id: SubmissionId
    artifact_id: ArtifactId
    storage_key: str
    byte_size: int


@dataclass(frozen=True)
class PurgePlan:
    """何を消すことになるか。**消す前に人へ見せるためのもの。**

    件数とバイト数を持つのは、空振りと 300 件の消去が同じ顔で終わっては
    いけないからである（`DemoReset` と同じ作法）。
    """

    now: datetime
    candidates: tuple[PurgeCandidate, ...] = ()
    #: 締切が無いので対象にしなかった課題の数（砂場・自習用）。
    tasks_without_deadline: int = 0
    #: まだ保存期間の中にある課題のうち、最も早く期限が来る時刻。
    next_expires_at: datetime | None = None

    @property
    def total_bytes(self) -> int:
        return sum(candidate.byte_size for candidate in self.candidates)

    def by_unit(self) -> list[tuple[str, int, int]]:
        """`(コース/問題セット, 件数, バイト数)` を期限の古い順に返す。"""
        groups: dict[str, list[PurgeCandidate]] = {}
        for candidate in self.candidates:
            label = f"{candidate.course_code} / {candidate.unit or '未分類'}"
            groups.setdefault(label, []).append(candidate)
        rows = [
            (label, len(items), sum(item.byte_size for item in items))
            for label, items in groups.items()
        ]
        return sorted(rows, key=lambda row: row[0])


@dataclass(frozen=True)
class PurgeOutcome:
    """実際に何が起きたか。"""

    deleted: int = 0
    freed_bytes: int = 0
    #: 消せなかったもの（ストアの障害）。**印は付けない。**
    failed: tuple[str, ...] = field(default=())


def plan_video_purge(
    database: Database,
    *,
    tenant_id: TenantId,
    now: datetime,
    course_id: CourseId | None = None,
) -> PurgePlan:
    """保存期間を過ぎた動画を集める。**何も消さない。**

    **締切の無い課題は対象にしない。** 起点が無いので期限も決まらない
    （`video_retention_expires_at` が `None` を返す）── 消し過ぎは取り返せず、
    消し残しは次に消せるので安全側に倒す。数だけ数えて呼び手に返す。
    """
    candidates: list[PurgeCandidate] = []
    without_deadline = 0
    next_expires_at: datetime | None = None

    with database.unit_of_work() as uow:
        courses: Sequence[Course]
        if course_id is None:
            courses = uow.identity.list_courses(tenant_id)
        else:
            course = uow.identity.get_course(course_id)
            if course is None:
                raise AdminError(f"コース {course_id!r} がありません")
            if course.tenant_id != tenant_id:
                raise AdminError("このテナントのコースではありません")
            courses = (course,)

        for course in courses:
            for task in uow.tasks.list_for_course(course.id):
                expires_at = video_retention_expires_at(task.due_at)
                if expires_at is None:
                    without_deadline += 1
                    continue
                if now < expires_at:
                    if next_expires_at is None or expires_at < next_expires_at:
                        next_expires_at = expires_at
                    continue
                # **提出は課題版ごとに引く**（#230）。コース全件を引いてから
                # 絞ると、`list_for_course` の上限の後ろに絞り込みが来る。
                version_ids = [version.id for version in uow.tasks.list_versions(task.id)]
                if not version_ids:
                    continue
                for submission in uow.submissions.list_for_versions(version_ids):
                    for artifact in submission.artifacts:
                        if artifact.kind is not ArtifactKind.VIDEO or artifact.is_purged:
                            continue
                        assert task.due_at is not None  # expires_at があるなら締切もある
                        candidates.append(
                            PurgeCandidate(
                                course_code=course.code,
                                unit=task.unit,
                                task_title=task.title,
                                due_at=task.due_at,
                                expires_at=expires_at,
                                submission_id=submission.id,
                                artifact_id=artifact.id,
                                storage_key=artifact.storage_key,
                                byte_size=artifact.byte_size,
                            )
                        )

    return PurgePlan(
        now=now,
        candidates=tuple(candidates),
        tasks_without_deadline=without_deadline,
        next_expires_at=next_expires_at,
    )


def purge_videos(
    database: Database,
    plan: PurgePlan,
    *,
    video_store: StreamingArtifactStore,
    tenant_id: TenantId,
    actor_id: UserId | None = None,
    now: datetime | None = None,
) -> PurgeOutcome:
    """下見で挙がったものを実際に消す。

    **ファイルを先に消し、消せたものにだけ印を付ける。** 逆にすると、印が
    付いてファイルが残った状態を次の実行が拾えない（印の付いたものは
    `plan_video_purge` が外す）。

    消せなかったものは印を付けずに返す ── 次の実行がもう一度拾う。
    """
    purged_at = now or plan.now
    deleted: list[tuple[SubmissionId, ArtifactId]] = []
    freed = 0
    failed: list[str] = []

    for candidate in plan.candidates:
        try:
            video_store.delete(candidate.storage_key)
        except Exception:  # pragma: no cover - ストアの実装差を吸収する
            failed.append(candidate.storage_key)
            continue
        deleted.append((candidate.submission_id, candidate.artifact_id))
        freed += candidate.byte_size

    if not deleted:
        return PurgeOutcome(failed=tuple(failed))

    with database.unit_of_work() as uow:
        marked = uow.submissions.mark_artifacts_purged(deleted, purged_at=purged_at)
        # **消した事実を残す**（ADR 0016）。学習者データそのものは入れない
        # ── 入れるのは件数と容量だけで、誰の動画だったかは提出の記録が持つ。
        recorder = (
            AuditRecorder.for_user(uow.audit, tenant_id=tenant_id, user_id=actor_id)
            if actor_id is not None
            else AuditRecorder.for_system(uow.audit, tenant_id=tenant_id)
        )
        recorder.record(
            AuditAction.VIDEO_PURGED,
            target_type="artifact",
            target_id=f"video:{purged_at.date().isoformat()}",
            summary=f"保存期間を過ぎた動画を {marked} 件消しました（{freed} バイト）",
            detail={"deleted": marked, "freed_bytes": freed, "failed": len(failed)},
        )
        uow.commit()

    return PurgeOutcome(deleted=marked, freed_bytes=freed, failed=tuple(failed))
