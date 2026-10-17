"""課題の提出を、**確定済みも含めて**いまの採点器で採点し直す（管理者の操作）。

## なぜ要るか

教員コンソールの「いまの版で再採点」は、確定していない提出のうち、古い版で
採点されたもの・採点に失敗したものだけを積む。**確定済みは動かさない**
── 確定は「この成績で閉じた」という事実で（ADR 0010）、あとから機械が採点し
直すと、確定の記録が指す採点と学習者に見える採点が食い違うからである。

ところが採点器の側の誤りで、同じ課題の中で採点の基準が割れることがある。
2026-10-16 の network ex4/paiza4・ex5/paiza5 では、画像を読むモデル
（`qwen3-vl:8b`）が考えすぎて書き起こしが打ち切られ（ADR 0021 §6）、123 件の
うち 55 件で画像の一部が読めなかった。教員が画像を見て採点した提出、読めて
いた提出、読めないまま自動確定した提出が、**それぞれ違う基準で閉じていた。**
全件を同じ採点器で採点し直すほうが公平だ、というのがこの操作の動機である。

## 何が起きるか（実行前に必ず下見で示す）

- 古い採点・人の採点（`HumanReview`）・確定（`Finalization`）は**消さない**
  （P8）。新しい採点が終わると旧採点に `superseded_by` が入り、成績は新しい
  採点になる。人の採点と確定は旧採点に付いたまま残り、新しい採点には効かない
- 新しい採点は確定していない状態から始まる。自動確定の猶予（課題・コース）は
  新しい採点の時刻から数え直すので、学習者はその間に再確認を依頼できる
- 遅延の減点は、提出時刻（変わらない）と、そのときの締切と規則で付け直す
- 人が採点する観点は「採点待ち」に戻る

**だから人の採点が消える件数を必ず先に出す。** 画面には置かない ── 確定した
成績を一括で開け直す操作を、教員がボタン 1 つで押せる場所に置かない。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import GradingRun, Submission, SubmissionState, Task, TaskVersion
from aijudge_core.ids import CourseId, TenantId
from aijudge_course_admin.errors import AdminError
from aijudge_submission import SubmissionService
from aijudge_unit_of_work import Store

#: 冪等キーの区別の頭。**いまの最新の採点の id を後ろに付ける**。同じ下見から
#: 2 度流しても、まだ採点し直していない提出には同じキーになるのでジョブは
#: 増えない。採点し直した後にもう一度流すと、最新の採点が変わっているので
#: 新しいジョブになる（`manage/task_ops.py` の評価器の失敗と同じ作り・#620）。
DISCRIMINATOR_PREFIX = "admin-regrade"

#: 理由の最短長。「再採点」の 3 文字で済ませない（`MIN_JUSTIFICATION_LENGTH` と同じ考え）。
MIN_REASON_LENGTH = 10


@dataclass(frozen=True)
class RegradeTarget:
    submission: Submission
    # 最新の採点。採点が 1 度も届いていない提出は None。
    run: GradingRun | None
    finalized: bool
    reviewed: bool


@dataclass(frozen=True)
class RegradePlan:
    task: Task
    # 採点に使う版。**学習者に出ている版**（コンソールの再採点と同じ・#48）。
    version: TaskVersion
    targets: tuple[RegradeTarget, ...]

    @property
    def finalized(self) -> int:
        return sum(1 for t in self.targets if t.finalized)

    @property
    def reviewed(self) -> int:
        return sum(1 for t in self.targets if t.reviewed)

    @property
    def without_run(self) -> int:
        return sum(1 for t in self.targets if t.run is None)


def find_task(database: Store, *, course_id: CourseId, task: str) -> Task:
    """課題を id か鍵（`ex4/paiza4`）で引く。**鍵が 2 つの課題に当たれば断る。**"""
    with database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(course_id)
        by_id = [t for t in tasks if str(t.id) == task]
        if by_id:
            return by_id[0]
        matched = []
        for candidate in tasks:
            latest = uow.tasks.latest_version(candidate.id)
            if latest is not None and latest.source_key == task:
                matched.append(candidate)
    if not matched:
        raise AdminError(f"課題 {task} がこのコースにありません")
    if len(matched) > 1:
        raise AdminError(
            f"課題の鍵 {task} が {len(matched)} 件に当たります（id で指定してください）"
        )
    return matched[0]


def plan_regrade(
    database: Store, *, tenant_id: TenantId, course_id: CourseId, task: str
) -> RegradePlan:
    """採点し直す提出と、消える判断の件数を数える。**何も書かない。**"""
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
        if course is None or course.tenant_id != tenant_id:
            # テナントをまたがない（`delete_course` と同じ判断）。
            raise AdminError(f"コース {course_id} がこのテナントにありません")
    found = find_task(database, course_id=course_id, task=task)
    with database.unit_of_work() as uow:
        version = uow.tasks.latest_published_version(found.id)
        if version is None:
            raise AdminError("承認済みの版がありません（先に承認してください）")
        versions = uow.tasks.list_versions(found.id)
        # **全部の版への提出を読む。** 訂正前の版に出した提出も対象にする
        # （上限のある `list_for_course` は使わない・#219）。
        submissions = uow.submissions.list_for_versions([v.id for v in versions])
        targets = []
        for submission in submissions:
            # 動作確認の提出は成績ではない（#108）。取り下げた提出は採点できない。
            if submission.is_trial or submission.state is not SubmissionState.SUBMITTED:
                continue
            run = uow.runs.latest_for(submission.id)
            targets.append(
                RegradeTarget(
                    submission=submission,
                    run=run,
                    finalized=run is not None
                    and uow.reviews.find_finalization_for_run(run.id) is not None,
                    reviewed=run is not None
                    and uow.reviews.find_review_for_run(run.id) is not None,
                )
            )
    return RegradePlan(task=found, version=version, targets=tuple(targets))


def execute_regrade(
    database: Store,
    service: SubmissionService,
    plan: RegradePlan,
    *,
    tenant_id: TenantId,
    reason: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """下見どおりに再採点を積む。**積むのはジョブだけ**で、採点はワーカーが走らせる。

    `service` は呼ぶ側（組み立ての層）が作って渡す。提出の受付と同じ経路で積む
    ので、冪等キーの作り方も受付の規則に従う。
    """
    reason = reason.strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise AdminError(f"理由を {MIN_REASON_LENGTH} 文字以上で書いてください")
    queued = 0
    for target in plan.targets:
        latest = "none" if target.run is None else str(target.run.id)
        service.request_regrade(
            tenant_id=tenant_id,
            submission_id=target.submission.id,
            # 回す先の版の規則で走らせる（コンソールの再採点と同じ・#195・#264）。
            subject_profile=plan.version.subject_profile,
            task_version_id=plan.version.id,
            discriminator=f"{DISCRIMINATOR_PREFIX}:{latest}",
        )
        queued += 1
    with database.unit_of_work() as uow:
        # **誰がではなく、何をなぜ**を残す。CLI は操作者を知らない（`_cli_audit`）。
        AuditRecorder.for_system(uow.audit, tenant_id=tenant_id, clock=clock).record(
            AuditAction.GRADES_REGRADED,
            target_type="task",
            target_id=str(plan.task.id),
            summary=(
                f"{plan.task.title} の提出 {queued} 件を、確定済み {plan.finalized} 件を"
                f"含めて採点し直しました（人の採点 {plan.reviewed} 件は新しい採点に効きません）"
            ),
            detail={
                "task_version_id": str(plan.version.id),
                "queued": queued,
                "finalized": plan.finalized,
                "reviewed": plan.reviewed,
                "reason": reason,
            },
        )
        uow.commit()
    return queued


def reviewed_submissions(plan: RegradePlan) -> Sequence[RegradeTarget]:
    """人の採点が新しい採点に効かなくなる提出。**あとで見比べる一覧のため。**"""
    return tuple(t for t in plan.targets if t.reviewed)
