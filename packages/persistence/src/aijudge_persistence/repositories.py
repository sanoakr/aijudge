"""S2 / S3 のプロトコルの SQLAlchemy 実装。

インメモリ実装と**同じ規則**を守る。ここで緩めると、テストが通るのに
本番で落ちる（あるいはその逆）。特に:

- `GradingRunRepository.save` は上書きを拒否する（P8）
- `supersede` は二度書き換えない
- `JobQueue.enqueue` は冪等キーで既存を返す
- `reserve` はリースの切れた RUNNING を取り直し、**行ロックで二重配布を防ぐ**

インメモリ実装との違いが 1 つある。同時実行である。アプリ側の
「あれば返す」だけでは、同時に来た 2 つのリクエストが両方「無い」を見て
両方作る。DB 側の一意制約と行ロックが最後の砦になる。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aijudge_authoring.repository import (
    TaskImmutabilityViolation,
    TaskStoreError,
    substantive,
)
from aijudge_authoring.verification import TaskChecks
from aijudge_core import (
    BlindMark,
    Finalization,
    GradingRun,
    HumanReview,
    ReviewRequest,
    ReviewState,
    Submission,
    Task,
    TaskVersion,
)
from aijudge_core.events import EVENT_TYPES, DomainEvent
from aijudge_core.ids import (
    CourseId,
    GradingJobId,
    GradingRunId,
    HumanReviewId,
    ReviewRequestId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_submission import GradingJob, GradingPhase, JobState
from aijudge_submission.protocols import (
    ImmutabilityViolation,
    RunDecision,
    SubmissionStoreError,
)

from .schema import (
    BlindMarkRow,
    FinalizationRow,
    GradingJobRow,
    GradingRunRow,
    HumanReviewRow,
    OutboxRow,
    ReviewRequestRow,
    SubmissionKeyRow,
    SubmissionRow,
    TaskChecksRow,
    TaskEmbeddingRow,
    TaskRow,
    TaskVersionRow,
)


def _dump(model: object) -> dict:
    return model.model_dump(mode="json")  # type: ignore[attr-defined]


class SqlSubmissionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, submission: Submission) -> None:
        existing = self._session.get(SubmissionRow, str(submission.id))
        if existing is not None:
            if existing.submitted_at is not None:
                raise ImmutabilityViolation(f"submission {submission.id} is already submitted")
            existing.state = submission.state.value
            existing.attempt = submission.attempt
            existing.submitted_at = submission.submitted_at
            existing.document = _dump(submission)
            return
        # tenant_id は Submission に無い（コアはテナントを持たない）。
        # 冪等キーの記録時に埋まる。ここでは空で入れ、下で更新する。
        self._session.add(
            SubmissionRow(
                id=str(submission.id),
                tenant_id="",
                task_version_id=str(submission.task_version_id),
                learner_id=str(submission.learner_id),
                state=submission.state.value,
                attempt=submission.attempt,
                created_at=submission.created_at,
                submitted_at=submission.submitted_at,
                document=_dump(submission),
            )
        )
        self._session.flush()

    def get(self, submission_id: SubmissionId) -> Submission | None:
        row = self._session.get(SubmissionRow, str(submission_id))
        return None if row is None else Submission.model_validate(row.document)

    def find_by_idempotency_key(self, tenant_id: TenantId, key: str) -> Submission | None:
        row = self._session.get(SubmissionKeyRow, (str(tenant_id), key))
        return None if row is None else self.get(SubmissionId(row.submission_id))

    def remember_idempotency_key(
        self, tenant_id: TenantId, key: str, submission_id: SubmissionId
    ) -> None:
        self._session.add(
            SubmissionKeyRow(
                tenant_id=str(tenant_id),
                idempotency_key=key,
                submission_id=str(submission_id),
            )
        )
        # テナントは提出行にも持たせる（テナント単位の走査に索引を使うため）。
        self._session.execute(
            update(SubmissionRow)
            .where(SubmissionRow.id == str(submission_id))
            .values(tenant_id=str(tenant_id))
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            # 同時に来た 2 つのリクエストが両方「無い」を見た場合。
            # 一意制約が止めるので、呼び出し側は再読み込みして既存を使う。
            raise ImmutabilityViolation(
                f"idempotency key {key!r} was claimed concurrently"
            ) from exc

    def list_for_learner(
        self,
        tenant_id: TenantId,
        learner_id: UserId,
        task_version_id: TaskVersionId | None = None,
    ) -> tuple[Submission, ...]:
        statement = (
            select(SubmissionRow)
            .where(
                SubmissionRow.tenant_id == str(tenant_id),
                SubmissionRow.learner_id == str(learner_id),
            )
            .order_by(SubmissionRow.created_at, SubmissionRow.id)
        )
        if task_version_id is not None:
            statement = statement.where(SubmissionRow.task_version_id == str(task_version_id))
        return tuple(
            Submission.model_validate(row.document)
            for row in self._session.execute(statement).scalars()
        )

    def list_for_course(self, course_id: CourseId, *, limit: int = 5000) -> tuple[Submission, ...]:
        """このコースの全提出。古い順。教員の一覧が読む。

        提出は課題版を指しており、コースを直接持たない（持たせると課題の
        移動で片方だけ古くなる）ので、課題 → コースの経路で絞る。
        """
        statement = (
            select(SubmissionRow)
            .join(TaskVersionRow, TaskVersionRow.id == SubmissionRow.task_version_id)
            .join(TaskRow, TaskRow.id == TaskVersionRow.task_id)
            .where(TaskRow.course_id == str(course_id))
            .order_by(SubmissionRow.created_at, SubmissionRow.id)
            .limit(limit)
        )
        return tuple(
            Submission.model_validate(row.document)
            for row in self._session.execute(statement).scalars()
        )

    def next_attempt(
        self, tenant_id: TenantId, learner_id: UserId, task_version_id: TaskVersionId
    ) -> int:
        return len(self.list_for_learner(tenant_id, learner_id, task_version_id)) + 1


class SqlGradingRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, run: GradingRun) -> None:
        if self._session.get(GradingRunRow, str(run.id)) is not None:
            raise ImmutabilityViolation(
                f"GradingRun {run.id} already exists; re-grading creates a new run (P8)"
            )
        self._session.add(
            GradingRunRow(
                id=str(run.id),
                submission_id=str(run.submission_id),
                task_version_id=str(run.context.task_version_id),
                subject_profile=run.context.subject_profile,
                input_hash=run.context.input_hash,
                score_ratio=run.score_ratio,
                confidence=run.confidence,
                routing=run.routing.value,
                superseded_by=None if run.superseded_by is None else str(run.superseded_by),
                created_at=run.created_at,
                document=_dump(run),
            )
        )
        self._session.flush()

    def get(self, run_id: GradingRunId) -> GradingRun | None:
        row = self._session.get(GradingRunRow, str(run_id))
        return None if row is None else GradingRun.model_validate(row.document)

    def list_for(self, submission_id: SubmissionId) -> tuple[GradingRun, ...]:
        rows = self._session.execute(
            select(GradingRunRow)
            .where(GradingRunRow.submission_id == str(submission_id))
            .order_by(GradingRunRow.created_at, GradingRunRow.id)
        ).scalars()
        return tuple(GradingRun.model_validate(row.document) for row in rows)

    def latest_for(self, submission_id: SubmissionId) -> GradingRun | None:
        runs = self.list_for(submission_id)
        return runs[-1] if runs else None

    def latest_for_many(
        self, submission_ids: Sequence[SubmissionId]
    ) -> dict[SubmissionId, GradingRun]:
        """複数の提出の最新採点を 1 クエリで引く。一覧画面のためにある。

        `unfinalized_for_task` と同じ「提出ごとの最新 1 件」の絞り方を使う。
        """
        if not submission_ids:
            return {}
        keys = [str(submission_id) for submission_id in submission_ids]
        latest = (
            select(
                GradingRunRow.submission_id.label("submission_id"),
                func.max(GradingRunRow.created_at).label("created_at"),
            )
            .where(GradingRunRow.submission_id.in_(keys))
            .group_by(GradingRunRow.submission_id)
            .subquery()
        )
        rows = self._session.execute(
            select(GradingRunRow)
            .join(
                latest,
                (latest.c.submission_id == GradingRunRow.submission_id)
                & (latest.c.created_at == GradingRunRow.created_at),
            )
            .order_by(GradingRunRow.created_at, GradingRunRow.id)
        ).scalars()
        # 同じ時刻の採点が 2 件あると 2 行返る。順序どおりに詰めれば
        # 後の（= `latest_for` が返すのと同じ）ものが残る。
        return {
            SubmissionId(row.submission_id): GradingRun.model_validate(row.document) for row in rows
        }

    def supersede(self, old_id: GradingRunId, new_id: GradingRunId) -> None:
        row = self._session.get(GradingRunRow, str(old_id))
        if row is None:
            raise SubmissionStoreError(f"no GradingRun {old_id}")
        if row.superseded_by is not None:
            raise ImmutabilityViolation(
                f"GradingRun {old_id} is already superseded by {row.superseded_by}"
            )
        row.superseded_by = str(new_id)
        document = dict(row.document)
        document["superseded_by"] = str(new_id)
        row.document = document
        self._session.flush()


class SqlReviewRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_review(self, review: HumanReview) -> None:
        run = self._session.get(GradingRunRow, str(review.grading_run_id))
        if run is None:
            raise SubmissionStoreError(f"no GradingRun {review.grading_run_id}")
        row = self._session.get(HumanReviewRow, str(review.id))
        if row is None:
            existing = (
                self._session.execute(
                    select(HumanReviewRow).where(
                        HumanReviewRow.grading_run_id == str(review.grading_run_id)
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                # 二度確定できると成績が二つ存在する。やり直しは再採点から。
                raise ImmutabilityViolation(
                    f"GradingRun {review.grading_run_id} is already finalised by "
                    f"{existing.grader_id}"
                )
            self._session.add(
                HumanReviewRow(
                    id=str(review.id),
                    grading_run_id=str(review.grading_run_id),
                    submission_id=str(run.submission_id),
                    grader_id=str(review.grader_id),
                    agreed=review.agreed,
                    reviewed_at=review.reviewed_at,
                    document=_dump(review),
                )
            )
        else:
            raise ImmutabilityViolation(f"HumanReview {review.id} already exists")
        self._session.flush()

    def get_review(self, review_id: HumanReviewId) -> HumanReview | None:
        row = self._session.get(HumanReviewRow, str(review_id))
        return None if row is None else HumanReview.model_validate(row.document)

    def find_review_for_run(self, run_id: GradingRunId) -> HumanReview | None:
        row = (
            self._session.execute(
                select(HumanReviewRow).where(HumanReviewRow.grading_run_id == str(run_id))
            )
            .scalars()
            .first()
        )
        return None if row is None else HumanReview.model_validate(row.document)

    def save_blind_mark(self, mark: BlindMark) -> None:
        if self._session.get(BlindMarkRow, str(mark.submission_id)) is not None:
            raise ImmutabilityViolation(
                f"submission {mark.submission_id} already has a blind mark; "
                "overwriting it would let a post-AI grade become ground truth (ADR 0005)"
            )
        self._session.add(
            BlindMarkRow(
                submission_id=str(mark.submission_id),
                grader_id=str(mark.grader_id),
                marked_at=mark.marked_at,
                document=_dump(mark),
            )
        )
        self._session.flush()

    def find_blind_mark(self, submission_id: SubmissionId) -> BlindMark | None:
        row = self._session.get(BlindMarkRow, str(submission_id))
        return None if row is None else BlindMark.model_validate(row.document)

    # -- 学習者からの再確認の依頼 ------------------------------------------

    def save_request(self, request: ReviewRequest) -> None:
        existing = (
            self._session.execute(
                select(ReviewRequestRow).where(
                    ReviewRequestRow.grading_run_id == str(request.grading_run_id)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise ImmutabilityViolation(
                f"GradingRun {request.grading_run_id} already has a review request"
            )
        self._session.add(
            ReviewRequestRow(
                id=str(request.id),
                grading_run_id=str(request.grading_run_id),
                submission_id=str(request.submission_id),
                learner_id=str(request.learner_id),
                requested_at=request.requested_at,
                resolved_by=None,
                document=_dump(request),
            )
        )
        self._session.flush()

    def find_request_for_run(self, run_id: GradingRunId) -> ReviewRequest | None:
        row = (
            self._session.execute(
                select(ReviewRequestRow).where(ReviewRequestRow.grading_run_id == str(run_id))
            )
            .scalars()
            .first()
        )
        return None if row is None else ReviewRequest.model_validate(row.document)

    def get_request(self, request_id: ReviewRequestId) -> ReviewRequest | None:
        row = self._session.get(ReviewRequestRow, str(request_id))
        return None if row is None else ReviewRequest.model_validate(row.document)

    def resolve_request(self, request_id: ReviewRequestId, review_id: HumanReviewId) -> None:
        row = self._session.get(ReviewRequestRow, str(request_id))
        if row is None:
            return
        row.resolved_by = str(review_id)
        document = dict(row.document)
        document["resolved_by"] = str(review_id)
        row.document = document
        self._session.flush()

    # -- レビュー待ち行列（教員 UI 用の読み取り）--------------------------

    def requested_for_course(
        self, course_id: CourseId, *, include_resolved: bool = False, limit: int = 200
    ) -> tuple[tuple[Submission, GradingRun, ReviewRequest], ...]:
        """このコースで**学習者が再確認を依頼した**提出。

        教員の待ち行列はこれである。全提出を並べると受講 91 名 × 課題数に
        なり、教員は何から見ればよいか分からない。AI の判定は採点直後に
        学習者へ示しているので、**疑いが出たものだけ**が人間の判断を要する。

        自発的に見たい提出は課題や学習者から辿る（依頼が無くても確定できる）。
        """
        statement = (
            select(SubmissionRow, GradingRunRow, ReviewRequestRow)
            .join(
                ReviewRequestRow,
                ReviewRequestRow.grading_run_id == GradingRunRow.id,
            )
            .join(SubmissionRow, SubmissionRow.id == GradingRunRow.submission_id)
            .join(TaskVersionRow, TaskVersionRow.id == SubmissionRow.task_version_id)
            .join(TaskRow, TaskRow.id == TaskVersionRow.task_id)
            .where(TaskRow.course_id == str(course_id))
            .order_by(ReviewRequestRow.requested_at, ReviewRequestRow.id)
            .limit(limit)
        )
        if not include_resolved:
            statement = statement.where(ReviewRequestRow.resolved_by.is_(None))

        return tuple(
            (
                Submission.model_validate(submission_row.document),
                GradingRun.model_validate(run_row.document),
                ReviewRequest.model_validate(request_row.document),
            )
            for submission_row, run_row, request_row in self._session.execute(statement).all()
        )

    # -- 成績の確定 --------------------------------------------------------

    def save_finalization(self, finalization: Finalization) -> None:
        run = self._session.get(GradingRunRow, str(finalization.grading_run_id))
        if run is None:
            raise SubmissionStoreError(f"no GradingRun {finalization.grading_run_id}")
        if self._session.get(FinalizationRow, str(finalization.id)) is not None:
            raise ImmutabilityViolation(f"Finalization {finalization.id} already exists")
        existing = (
            self._session.execute(
                select(FinalizationRow).where(
                    FinalizationRow.grading_run_id == str(finalization.grading_run_id)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            # 二度確定できると成績が二つ存在する。やり直しは再採点から。
            raise ImmutabilityViolation(
                f"GradingRun {finalization.grading_run_id} is already finalised ({existing.source})"
            )
        self._session.add(
            FinalizationRow(
                id=str(finalization.id),
                grading_run_id=str(finalization.grading_run_id),
                submission_id=str(run.submission_id),
                source=finalization.source.value,
                actor_id=None if finalization.actor_id is None else str(finalization.actor_id),
                finalized_at=finalization.finalized_at,
                document=_dump(finalization),
            )
        )
        self._session.flush()

    def find_finalization_for_run(self, run_id: GradingRunId) -> Finalization | None:
        row = (
            self._session.execute(
                select(FinalizationRow).where(FinalizationRow.grading_run_id == str(run_id))
            )
            .scalars()
            .first()
        )
        return None if row is None else Finalization.model_validate(row.document)

    def decisions_for_runs(
        self, run_ids: Sequence[GradingRunId]
    ) -> dict[GradingRunId, RunDecision]:
        """複数の採点のレビュー・依頼・確定をまとめて引く。一覧画面のためにある。

        3 つの表を外部結合せず 3 クエリに分ける。どれも採点 1 件につき
        高々 1 行なので結合しても正しいが、分けた方が読める。
        """
        if not run_ids:
            return {}
        keys = [str(run_id) for run_id in run_ids]
        reviews = {
            GradingRunId(row.grading_run_id): HumanReview.model_validate(row.document)
            for row in self._session.execute(
                select(HumanReviewRow).where(HumanReviewRow.grading_run_id.in_(keys))
            ).scalars()
        }
        requests = {
            GradingRunId(row.grading_run_id): ReviewRequest.model_validate(row.document)
            for row in self._session.execute(
                select(ReviewRequestRow).where(ReviewRequestRow.grading_run_id.in_(keys))
            ).scalars()
        }
        finalizations = {
            GradingRunId(row.grading_run_id): Finalization.model_validate(row.document)
            for row in self._session.execute(
                select(FinalizationRow).where(FinalizationRow.grading_run_id.in_(keys))
            ).scalars()
        }
        return {
            run_id: RunDecision(
                review=reviews.get(run_id),
                request=requests.get(run_id),
                finalization=finalizations.get(run_id),
            )
            for run_id in reviews.keys() | requests.keys() | finalizations.keys()
        }

    def unfinalized_for_task(
        self, task_id: TaskId, *, limit: int = 500
    ) -> tuple[tuple[Submission, GradingRun, ReviewRequest | None], ...]:
        """この課題でまだ確定していない提出。一括確定と自動確定が読む。

        **最新の採点 1 件につき 1 行。** 再採点された提出で古い採点まで
        確定させると、学習者に見えている点と確定した点が食い違う。

        未対応の異議申立があるものを呼び出し側が外せるよう、依頼も返す
        （`aijudge_core.blocks_finalization`）。
        """
        latest = (
            select(
                GradingRunRow.submission_id.label("submission_id"),
                func.max(GradingRunRow.created_at).label("created_at"),
            )
            .group_by(GradingRunRow.submission_id)
            .subquery()
        )
        finalised = select(FinalizationRow.grading_run_id)
        statement = (
            select(SubmissionRow, GradingRunRow, ReviewRequestRow)
            .join(TaskVersionRow, TaskVersionRow.id == SubmissionRow.task_version_id)
            .join(GradingRunRow, GradingRunRow.submission_id == SubmissionRow.id)
            .join(
                latest,
                (latest.c.submission_id == GradingRunRow.submission_id)
                & (latest.c.created_at == GradingRunRow.created_at),
            )
            .outerjoin(ReviewRequestRow, ReviewRequestRow.grading_run_id == GradingRunRow.id)
            .where(TaskVersionRow.task_id == str(task_id))
            .where(GradingRunRow.id.not_in(finalised))
            .order_by(SubmissionRow.submitted_at, SubmissionRow.id)
            .limit(limit)
        )
        return tuple(
            (
                Submission.model_validate(submission_row.document),
                GradingRun.model_validate(run_row.document),
                None if request_row is None else ReviewRequest.model_validate(request_row.document),
            )
            for submission_row, run_row, request_row in self._session.execute(statement).all()
        )

    def pending_for_course(
        self, course_id: CourseId, *, include_decided: bool = False, limit: int = 200
    ) -> tuple[tuple[Submission, GradingRun], ...]:
        """このコースで教員の確認を待っている提出。

        「採点が届いていて、まだ確定していない」もの。採点が届いていない
        提出をレビュー画面に出さないのは、そこで採点を起動しないため
        （ADR 0007）。提出のたびに 1 行ではなく、**最新の採点 1 件につき 1 行**。

        課題 → コースの経路で絞る。提出は課題版を指しており、コースを
        直接持たない（持たせると課題の移動で片方だけ古くなる）。
        """
        latest = (
            select(
                GradingRunRow.submission_id.label("submission_id"),
                func.max(GradingRunRow.created_at).label("created_at"),
            )
            .group_by(GradingRunRow.submission_id)
            .subquery()
        )
        statement = (
            select(SubmissionRow, GradingRunRow)
            .join(
                TaskVersionRow,
                TaskVersionRow.id == SubmissionRow.task_version_id,
            )
            .join(TaskRow, TaskRow.id == TaskVersionRow.task_id)
            .join(GradingRunRow, GradingRunRow.submission_id == SubmissionRow.id)
            .join(
                latest,
                (latest.c.submission_id == GradingRunRow.submission_id)
                & (latest.c.created_at == GradingRunRow.created_at),
            )
            .where(TaskRow.course_id == str(course_id))
            .order_by(SubmissionRow.submitted_at, SubmissionRow.id)
            .limit(limit)
        )
        if not include_decided:
            # 確定の有無は Finalization で見る。HumanReview は「教員が読んだ」
            # 記録であって確定ではない（ADR 0010）。
            finalised = select(FinalizationRow.grading_run_id)
            statement = statement.where(GradingRunRow.id.not_in(finalised))

        return tuple(
            (
                Submission.model_validate(submission_row.document),
                GradingRun.model_validate(run_row.document),
            )
            for submission_row, run_row in self._session.execute(statement).all()
        )


class SqlJobQueue:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, job: GradingJob) -> GradingJob:
        existing = self.find_by_idempotency_key(job.idempotency_key)
        if existing is not None:
            return existing
        self._session.add(_job_row(job))
        try:
            self._session.flush()
        except IntegrityError:
            # 同時投入。一意制約が止めた側は既存を読んで返す。
            self._session.rollback()
            existing = self.find_by_idempotency_key(job.idempotency_key)
            if existing is None:  # pragma: no cover - 制約違反の原因が別
                raise
            return existing
        return job

    def reserve(
        self,
        now: datetime,
        *,
        worker: str,
        lease_seconds: float,
        subject_profile: str | None = None,
        phase: GradingPhase | None = None,
    ) -> GradingJob | None:
        """実行可能なジョブを 1 つ取る。

        **行ロックを取る**（`with_for_update(skip_locked=True)`）。取らないと、
        複数のワーカーが同じジョブを読んで両方が採点し、同じ提出に対する
        GradingRun が二重にできる。`skip_locked` なのは、他のワーカーが
        見ている行を待たずに次の仕事へ行くため。
        """
        queued = select(GradingJobRow).where(
            GradingJobRow.state == JobState.QUEUED.value,
            GradingJobRow.available_at <= now,
        )
        expired = select(GradingJobRow).where(
            GradingJobRow.state == JobState.RUNNING.value,
            GradingJobRow.lease_expires_at.is_not(None),
            GradingJobRow.lease_expires_at <= now,
        )
        if subject_profile is not None:
            queued = queued.where(GradingJobRow.subject_profile == subject_profile)
            expired = expired.where(GradingJobRow.subject_profile == subject_profile)
        if phase is not None:
            # 段階を絞ったワーカーは、遅い段階の後ろに並ばない。これが
            # 「決定的評価の結果を先に返す」の実体（GradingPhase 参照）。
            queued = queued.where(GradingJobRow.phase == phase.value)
            expired = expired.where(GradingJobRow.phase == phase.value)

        for statement in (queued, expired):
            statement = statement.order_by(
                GradingJobRow.available_at, GradingJobRow.created_at, GradingJobRow.id
            ).limit(1)
            if self._session.bind is not None and self._session.bind.dialect.name != "sqlite":
                # SQLite は行ロックを持たない。単一プロセスの開発用なので許容する。
                statement = statement.with_for_update(skip_locked=True)
            row = self._session.execute(statement).scalars().first()
            if row is None:
                continue
            job = GradingJob.model_validate(row.document)
            reserved = job.reserved(now, worker=worker, lease_seconds=lease_seconds)
            _apply(row, reserved)
            self._session.flush()
            return reserved
        return None

    def update(self, job: GradingJob) -> None:
        row = self._session.get(GradingJobRow, str(job.id))
        if row is None:
            raise SubmissionStoreError(f"no job {job.id}")
        _apply(row, job)
        self._session.flush()

    def get(self, job_id: GradingJobId) -> GradingJob | None:
        row = self._session.get(GradingJobRow, str(job_id))
        return None if row is None else GradingJob.model_validate(row.document)

    def find_by_idempotency_key(self, key: str) -> GradingJob | None:
        row = (
            self._session.execute(select(GradingJobRow).where(GradingJobRow.idempotency_key == key))
            .scalars()
            .first()
        )
        return None if row is None else GradingJob.model_validate(row.document)

    def awaiting(self, submission_id: SubmissionId, phase: GradingPhase) -> bool:
        statement = select(GradingJobRow).where(
            GradingJobRow.submission_id == str(submission_id),
            GradingJobRow.phase == phase.value,
            GradingJobRow.state.in_((JobState.QUEUED.value, JobState.RUNNING.value)),
        )
        return self._session.execute(statement).scalars().first() is not None

    def pending_count(
        self, subject_profile: str | None = None, phase: GradingPhase | None = None
    ) -> int:
        statement = select(GradingJobRow).where(
            GradingJobRow.state.in_((JobState.QUEUED.value, JobState.RUNNING.value))
        )
        if subject_profile is not None:
            statement = statement.where(GradingJobRow.subject_profile == subject_profile)
        if phase is not None:
            statement = statement.where(GradingJobRow.phase == phase.value)
        return len(self._session.execute(statement).scalars().all())


def _job_row(job: GradingJob) -> GradingJobRow:
    return GradingJobRow(
        id=str(job.id),
        tenant_id=str(job.tenant_id),
        submission_id=str(job.submission_id),
        subject_profile=job.subject_profile,
        reason=job.reason.value,
        phase=job.phase.value,
        idempotency_key=job.idempotency_key,
        state=job.state.value,
        attempts=job.attempts,
        available_at=job.available_at,
        lease_expires_at=job.lease_expires_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        document=_dump(job),
    )


def _apply(row: GradingJobRow, job: GradingJob) -> None:
    row.state = job.state.value
    row.attempts = job.attempts
    row.available_at = job.available_at
    row.lease_expires_at = job.lease_expires_at
    row.updated_at = job.updated_at
    row.document = _dump(job)


class SqlOutbox:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: DomainEvent) -> None:
        self._session.add(
            OutboxRow(
                event_id=str(event.event_id),
                tenant_id=str(event.tenant_id),
                type=event.type,
                occurred_at=event.occurred_at,
                published_at=None,
                document=_dump(event),
            )
        )
        self._session.flush()

    def unpublished(self, limit: int = 100) -> tuple[DomainEvent, ...]:
        rows = self._session.execute(
            select(OutboxRow)
            .where(OutboxRow.published_at.is_(None))
            .order_by(OutboxRow.occurred_at, OutboxRow.event_id)
            .limit(limit)
        ).scalars()
        return tuple(_revive(row) for row in rows)

    def mark_published(self, event_ids: Sequence[str]) -> None:
        if not event_ids:
            return
        from datetime import UTC

        self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.event_id.in_([str(item) for item in event_ids]))
            .values(published_at=datetime.now(UTC))
        )
        self._session.flush()


def _revive(row: OutboxRow) -> DomainEvent:
    event_type = EVENT_TYPES.get(row.type)
    if event_type is None:
        raise SubmissionStoreError(f"unknown event type in the outbox: {row.type!r}")
    return event_type.model_validate(row.document)  # type: ignore[return-value]


class SqlTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_task(self, task: Task) -> None:
        row = self._session.get(TaskRow, str(task.id))
        if row is None:
            self._session.add(
                TaskRow(
                    id=str(task.id),
                    course_id=str(task.course_id),
                    unit=task.unit,
                    session=task.session,
                    position=task.position,
                    document=_dump(task),
                )
            )
        else:
            row.course_id = str(task.course_id)
            row.unit = task.unit
            row.session = task.session
            row.position = task.position
            row.document = _dump(task)
        self._session.flush()

    def get_task(self, task_id: TaskId) -> Task | None:
        row = self._session.get(TaskRow, str(task_id))
        return None if row is None else Task.model_validate(row.document)

    def save_version(self, version: TaskVersion) -> None:
        row = self._session.get(TaskVersionRow, str(version.id))
        if row is not None:
            if substantive(TaskVersion.model_validate(row.document)) == substantive(version):
                # 同じ内容の取り込みは冪等。決定的 ID の経路で普通に起きる。
                # 判定から `created_at` を外している（authoring/repository.py 参照）。
                return
            raise TaskImmutabilityViolation(
                f"TaskVersion {version.id} already exists with different content; "
                "corrections create a new version (P8)"
            )
        self._session.add(
            TaskVersionRow(
                id=str(version.id),
                task_id=str(version.task_id),
                version=version.version,
                subject_profile=version.subject_profile,
                review_state=version.provenance.review_state.value,
                allow_handwriting=version.allow_handwriting,
                statement=version.statement,
                created_at=version.created_at,
                document=_dump(version),
            )
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise TaskStoreError(
                f"task {version.task_id} already has a version {version.version}"
            ) from exc

    def get_version(self, version_id: TaskVersionId) -> TaskVersion | None:
        row = self._session.get(TaskVersionRow, str(version_id))
        return None if row is None else TaskVersion.model_validate(row.document)

    def latest_version(self, task_id: TaskId) -> TaskVersion | None:
        row = (
            self._session.execute(
                select(TaskVersionRow)
                .where(TaskVersionRow.task_id == str(task_id))
                .order_by(TaskVersionRow.version.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        return None if row is None else TaskVersion.model_validate(row.document)

    def save_checks(self, version_id: TaskVersionId, checks: TaskChecks) -> None:
        """検査の結果を残す。**上書きしてよい**（課題版と違い測り直せる）。"""
        row = self._session.get(TaskChecksRow, str(version_id))
        document = checks.model_dump(mode="json")
        if row is None:
            self._session.add(
                TaskChecksRow(
                    task_version_id=str(version_id),
                    usable=checks.verification.usable,
                    checked_at=checks.checked_at,
                    document=document,
                )
            )
        else:
            row.usable = checks.verification.usable
            row.checked_at = checks.checked_at
            row.document = document
        self._session.flush()

    def get_checks(self, version_id: TaskVersionId) -> TaskChecks | None:
        row = self._session.get(TaskChecksRow, str(version_id))
        return None if row is None else TaskChecks.model_validate(row.document)

    def save_embedding(
        self,
        version_id: TaskVersionId,
        *,
        model: str,
        subject_profile: str,
        vector: tuple[float, ...],
    ) -> None:
        row = self._session.get(TaskEmbeddingRow, (str(version_id), model))
        values = [float(v) for v in vector]
        if row is None:
            self._session.add(
                TaskEmbeddingRow(
                    task_version_id=str(version_id),
                    model=model,
                    subject_profile=subject_profile,
                    dimensions=len(values),
                    vector=values,
                )
            )
        else:
            row.dimensions = len(values)
            row.vector = values
        self._session.flush()

    def list_embeddings(self, *, model: str, subject_profile: str) -> dict[str, tuple[float, ...]]:
        """同じモデル・同じ科目のベクトルだけを返す。

        **モデルを跨いで混ぜない。** 次元が同じでも意味空間が違うので、
        混ぜると無関係な課題が似ていることになる。
        """
        rows = self._session.execute(
            select(TaskEmbeddingRow).where(
                TaskEmbeddingRow.model == model,
                TaskEmbeddingRow.subject_profile == subject_profile,
            )
        ).scalars()
        return {row.task_version_id: tuple(float(v) for v in row.vector) for row in rows}

    def pass_rates(
        self, version_ids: tuple[TaskVersionId, ...], *, threshold: float
    ) -> dict[str, tuple[int, int]]:
        """課題版ごとの `(提出数, 通った数)`。難度推定の材料。

        **最新の採点だけを数える**（`superseded_by` が空のもの）。二段階
        キューは 1 提出につき採点を 2 つ作るので、素直に数えると提出数が
        倍になる（ADR 0011）。

        **提出そのものではなく採点を数えている。** 採点されていない提出は
        入らない ── 通ったかどうかが分からないものを分母に入れると、
        正答率が実際より低く出る。
        """
        if not version_ids:
            return {}
        rows = self._session.execute(
            select(
                GradingRunRow.task_version_id,
                func.count(GradingRunRow.id),
                func.sum(case((GradingRunRow.score_ratio >= threshold, 1), else_=0)),
            )
            .where(
                GradingRunRow.task_version_id.in_([str(v) for v in version_ids]),
                GradingRunRow.superseded_by.is_(None),
            )
            .group_by(GradingRunRow.task_version_id)
        ).all()
        return {row[0]: (int(row[1]), int(row[2] or 0)) for row in rows}

    def list_versions_in_review(self) -> tuple[TaskVersion, ...]:
        """教員のレビュー待ち。**生成物が溜まる場所。**"""
        rows = self._session.execute(
            select(TaskVersionRow)
            .where(TaskVersionRow.review_state == ReviewState.IN_REVIEW.value)
            .order_by(TaskVersionRow.created_at, TaskVersionRow.id)
        ).scalars()
        return tuple(TaskVersion.model_validate(row.document) for row in rows)

    def record_review(
        self,
        version_id: TaskVersionId,
        *,
        approved: bool,
        reviewer: UserId,
        reason: str | None,
    ) -> TaskVersion:
        """レビューの結果だけを書き戻す。

        **`save_version` と別の口にしてある。** 同じ口にすると、レビューの
        つもりで問題文を差し替えられる（出題済みの課題が黙って変わる）。
        ここが触るのは `review_state` / `reviewed_by` / `reject_reason` だけ。
        """
        row = self._session.get(TaskVersionRow, str(version_id))
        if row is None:
            raise TaskStoreError(f"課題版が見つかりません: {version_id}")
        version = TaskVersion.model_validate(row.document)
        updated = version.model_copy(
            update={
                "provenance": version.provenance.reviewed(
                    approved=approved, reviewer=reviewer, reason=reason
                )
            }
        )
        row.review_state = updated.provenance.review_state.value
        row.document = _dump(updated)
        self._session.flush()
        return updated

    def list_for_course(self, course_id: CourseId) -> tuple[Task, ...]:
        rows = self._session.execute(
            select(TaskRow)
            .where(TaskRow.course_id == str(course_id))
            # 何回目 → まとまり → その中の順。回の無い課題（試験など）は後ろ。
            .order_by(
                TaskRow.session.is_(None),
                TaskRow.session,
                TaskRow.unit,
                TaskRow.position,
                TaskRow.id,
            )
        ).scalars()
        return tuple(Task.model_validate(row.document) for row in rows)
