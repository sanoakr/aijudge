"""採点ワーカー。

キューに積まれたジョブを取り、採点し、結果を保存してイベントを出す。
**S3 と S5 を束ねるのはここだけ。** 提出受付は採点エンジンを知らず、
採点エンジンはキューを知らない（ADR 0001 / 設計方針 §2.3）。

1 ジョブの流れ:

    予約（リースを取って commit）
      → 提出と課題を読む
      → 採点する
      → 採点結果を保存（追記のみ）／旧採点に superseded_by を記す
      → 観測を書く（失敗しても採点は成立させる）
      → GradingCompleted を outbox に積む
      → ジョブを DONE にして commit

予約だけ先に commit するのが要点。採点は数十秒かかるので、その間
リースが他のワーカーから見えていなければ二重に採点される。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aijudge_authoring.repository import TaskStoreError
from aijudge_core import (
    Course,
    GradingPhase,
    GradingRun,
    Routing,
    Submission,
    Task,
    TaskVersion,
    final_score,
    late_penalty_for,
    new_id,
    penalty_crosses_boundary,
)
from aijudge_core.ids import GradingJobId
from aijudge_feedback import FeedbackGenerator
from aijudge_grading import (
    EvaluatorRegistry,
    GradingPipeline,
    SubjectProfile,
    grading_completed_event,
    load_profile,
    project_observations,
)
from aijudge_persistence import Database, ObservationFileStore
from aijudge_submission import (
    ArtifactStore,
    GradingJob,
    JobReason,
    gradable_contents,
    job_idempotency_key,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 900.0


class PermanentGradingError(Exception):
    """何度やっても同じ失敗。リトライせず打ち切る。

    課題定義が無い、科目プロファイルに存在しない評価器が書いてある、といった
    構成の誤り。リトライしても GPU を無駄に回すだけで、直るのは人間が
    設定を直したときだけ。
    """


@dataclass(frozen=True)
class WorkResult:
    """1 ジョブの処理結果。"""

    job: GradingJob
    run: GradingRun | None = None
    error: str | None = None

    @property
    def graded(self) -> bool:
        return self.run is not None


class GradingWorker:
    def __init__(
        self,
        database: Database,
        artifact_store: ArtifactStore,
        *,
        profiles_dir: Path,
        registry: EvaluatorRegistry | None = None,
        observations: ObservationFileStore | None = None,
        feedback: FeedbackGenerator | None = None,
        worker: str = "worker-1",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._store = artifact_store
        self._profiles_dir = profiles_dir
        self._registry = registry or EvaluatorRegistry().load_installed()
        self._observations = observations
        self._feedback = feedback
        self._worker = worker
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._profiles: dict[str, SubjectProfile] = {}
        # 決定的段階のあとに積む AI 段階のジョブ。1 件処理するあいだだけ持つ。
        self._follow_up: tuple[GradingJob, TaskVersion] | None = None

    # -- 1 件処理 ----------------------------------------------------------

    def run_once(
        self,
        *,
        subject_profile: str | None = None,
        phase: GradingPhase | None = None,
    ) -> WorkResult | None:
        """ジョブを 1 つ処理する。無ければ None。

        `phase` を渡すとその段階のジョブだけを取る。**決定的評価専用の
        ワーカーを立てるためにある** ── 立てないと、テスト実行が終わって
        いる提出の結果が、前に並んだ他人の LLM 待ちの後ろで止まる。
        """
        now = self._clock()

        # --- 予約。ここで commit してリースを他のワーカーに見せる。 -------
        with self._database.unit_of_work() as uow:
            job = uow.jobs.reserve(
                now,
                worker=self._worker,
                lease_seconds=self._lease_seconds,
                subject_profile=subject_profile,
                phase=phase,
            )
            if job is None:
                return None
            uow.commit()

        try:
            run = self._grade(job)
        except PermanentGradingError as exc:
            return self._record_failure(job, str(exc), permanent=True)
        except Exception as exc:  # 一時的な失敗として扱い、上限までリトライする
            logger.exception("grading failed for job %s", job.id)
            return self._record_failure(job, f"{type(exc).__name__}: {exc}")

        return WorkResult(job=self._record_success(job, run), run=run)

    def run_until_empty(
        self,
        *,
        subject_profile: str | None = None,
        phase: GradingPhase | None = None,
        limit: int = 1000,
    ) -> tuple[int, tuple[str, ...]]:
        """キューが空になるまで処理する。

        1 件の失敗で止めない。締切前に 1 件の異常提出で全員の採点が
        止まるのは受け入れられない。
        """
        graded = 0
        errors: list[str] = []
        for _ in range(limit):
            result = self.run_once(subject_profile=subject_profile, phase=phase)
            if result is None:
                break
            if result.graded:
                graded += 1
            else:
                errors.append(f"{result.job.submission_id}: {result.error}")
        return graded, tuple(errors)

    # -- internals ---------------------------------------------------------

    def _grade(self, job: GradingJob) -> GradingRun:
        with self._database.unit_of_work() as uow:
            submission = uow.submissions.get(job.submission_id)
            if submission is None:
                raise PermanentGradingError(f"提出が見つかりません: {job.submission_id}")
            task_version = uow.tasks.get_version(job.task_version_id)
            if task_version is None:
                raise PermanentGradingError(f"課題版が見つかりません: {job.task_version_id}")
            # 遅延の減点に要る。**採点には渡さない** ── 評価器も採点エンジンも
            # 締切を知らないままにする（評価は遅延と独立、ADR 0013）。
            task = uow.tasks.get_task(task_version.task_id)
            course = None if task is None else uow.identity.get_course(task.course_id)

        base: GradingRun | None = None
        if job.phase is GradingPhase.AI:
            with self._database.unit_of_work() as uow:
                base = uow.runs.get(job.base_run_id)
            if base is None:
                raise PermanentGradingError(f"土台の採点が見つかりません: {job.base_run_id}")

        profile = self._profile(job.subject_profile)
        contents = gradable_contents(submission, self._store)
        pipeline = GradingPipeline(self._registry, profile)
        run = pipeline.run(
            task_version,
            submission,
            lambda artifact: contents[artifact.id],
            phase=job.phase,
            base=base,
        )
        run = self._with_feedback(run, task_version, contents)
        run = self._with_late_penalty(
            run,
            submission=submission,
            task=task,
            course=course,
            task_version=task_version,
            profile=profile,
        )

        if job.phase is GradingPhase.DETERMINISTIC and pipeline.has_ai_work(task_version, run):
            # AI 段階を積む。**決定的段階の結果を保存してから**（`_record_success`）
            # にしないと、土台がまだ無いジョブが走りうる。ここでは印だけ付ける。
            self._follow_up = (job, task_version)
        else:
            self._follow_up = None
        return run

    def _with_late_penalty(
        self,
        run: GradingRun,
        *,
        submission: Submission,
        task: Task | None,
        course: Course | None,
        task_version: TaskVersion,
        profile: SubjectProfile,
    ) -> GradingRun:
        """遅延の減点を付ける。**採点したあとに、採点の外から当てる。**

        ここに置くのは、ワーカーが S3（提出・締切）と S5（採点）を束ねる
        唯一の場所だからである。採点エンジンに締切を渡すと、評価が遅延を
        知ってしまう（ADR 0013）。

        **保存前に確定させて記録する**（`GradingRun` は保存後不変、P8）。
        表示のたびに計算し直す作りにすると、教員が学期の途中で規則を変えた
        瞬間に過去の成績が黙って動く。
        """
        if task is None or course is None:
            return run
        penalty = late_penalty_for(
            task.due_at, submission.deadline_timestamp, course.late_penalty_steps
        )
        if penalty is None:
            return run

        penalized = run.model_copy(update={"penalty": penalty})
        score = final_score(penalized, task_version)
        if penalty_crosses_boundary(score, profile.review_policy.boundary_score):
            # 評価としては及第だったものが、遅延だけで不可になる。自動で
            # 閉じずに教員が見る（P5）。遅延の事実に迷いは無いが、免除する
            # かどうかの判断は人にしか無い。
            penalized = penalized.model_copy(update={"routing": Routing.REVIEW_REQUIRED})
        return penalized

    def _with_feedback(
        self, run: GradingRun, task_version: TaskVersion, contents: dict
    ) -> GradingRun:
        """フィードバックを付ける。

        保存前に付けるのが要点。`GradingRun` は保存後不変（P8）なので、
        あとから書き足す経路を作らない。

        **失敗しても採点は成立させる。** フィードバックが出ないことは
        採点の失敗ではない。
        """
        if self._feedback is None:
            return run
        try:
            source = next((payload.decode("utf-8", "replace") for payload in contents.values()), "")
            result = self._feedback.generate(run, task_version, source)
        except Exception:
            logger.warning("could not generate feedback", exc_info=True)
            return run
        if result is None:
            return run
        if result.redundant:
            # 観点の説明をそのまま並べただけのものは付けない。学習者の画面で
            # 同じ文章が 2 度出る（実測で確認）。LLM が使える環境では
            # `redundant` が立たないので、そのときは付く。
            return run
        return run.model_copy(update={"feedback": result.message})

    def _profile(self, name: str) -> SubjectProfile:
        if name not in self._profiles:
            path = self._profiles_dir / f"{name}.yaml"
            if not path.is_file():
                raise PermanentGradingError(f"科目プロファイルがありません: {path}")
            try:
                self._profiles[name] = load_profile(path, self._registry)
            except Exception as exc:
                # 存在しない評価器名が書いてある等。人間が直すまで直らない。
                raise PermanentGradingError(f"科目プロファイルが不正です: {exc}") from exc
        return self._profiles[name]

    def _record_success(self, job: GradingJob, run: GradingRun) -> GradingJob:
        now = self._clock()
        with self._database.unit_of_work() as uow:
            previous = uow.runs.latest_for(job.submission_id)
            uow.runs.save(run)
            if previous is not None and previous.superseded_by is None:
                # 再採点。過去の採点は消さず、置き換わったことだけ記す（P8）。
                uow.runs.supersede(previous.id, run.id)

            submission = uow.submissions.get(job.submission_id)
            if submission is not None:
                uow.outbox.append(grading_completed_event(run, submission, tenant_id=job.tenant_id))
            done = job.completed(now, run.id)
            uow.jobs.update(done)

            follow_up = self._follow_up
            if follow_up is not None and follow_up[0].id == job.id:
                # AI 段階を同じトランザクションで積む。別にすると、決定的評価
                # だけ保存されて AI が永久に来ない提出ができる。
                uow.jobs.enqueue(self._ai_job(job, run, now))
            uow.commit()

        # 観測は採点の外側。失敗しても採点は成立させる（ADR 0007）。
        #
        # **暫定の採点では書かない。** 決定的段階の run は AI 観点が未採点
        # なので、それを測定に入れると「AI が判定しなかった」という記録が
        # 一致度の標本に混ざる。AI 段階が続くならその後で書く。
        if self._follow_up is None:
            self._record_observations(job, run)
        return done

    def _ai_job(self, job: GradingJob, base: GradingRun, now: datetime) -> GradingJob:
        """決定的評価の結果の上に積む AI 段階のジョブ。"""
        return GradingJob(
            id=GradingJobId(new_id("job")),
            tenant_id=job.tenant_id,
            submission_id=job.submission_id,
            task_version_id=job.task_version_id,
            subject_profile=job.subject_profile,
            reason=job.reason,
            phase=GradingPhase.AI,
            base_run_id=base.id,
            idempotency_key=job_idempotency_key(
                job.submission_id, job.reason, phase=GradingPhase.AI
            ),
            available_at=now,
            created_at=now,
            updated_at=now,
        )

    def _record_failure(
        self, job: GradingJob, error: str, *, permanent: bool = False
    ) -> WorkResult:
        now = self._clock()
        with self._database.unit_of_work() as uow:
            updated = job.failed(now, error, permanent=permanent)
            uow.jobs.update(updated)
            uow.commit()
        return WorkResult(job=updated, error=error)

    def _record_observations(self, job: GradingJob, run: GradingRun) -> None:
        if self._observations is None:
            return
        try:
            with self._database.unit_of_work() as uow:
                submission = uow.submissions.get(job.submission_id)
                task_version = uow.tasks.get_version(job.task_version_id)
            if submission is None or task_version is None:  # pragma: no cover - 直前に読めている
                return
            self._observations.save(
                project_observations(
                    run,
                    task_version,
                    subject_profile=job.subject_profile,
                    task_name=str(task_version.task_id),
                    submission=str(submission.id),
                    observed_at=self._clock(),
                )
            )
        except Exception:
            # 測定の都合で採点を落とさない。書けなかったことは次の採点や
            # レビュー確定のタイミングで補われる。
            logger.warning("could not record observations for job %s", job.id, exc_info=True)


def import_task_version(
    database: Database, task_version: TaskVersion, task: object | None = None
) -> None:
    """課題を保存する。取り込みスクリプトと UI から使う。

    同じ内容の再取り込みは冪等（決定的 ID の経路で普通に起きる）。
    """
    with database.unit_of_work() as uow:
        if task is not None:
            uow.tasks.save_task(task)  # type: ignore[arg-type]
        try:
            uow.tasks.save_version(task_version)
        except TaskStoreError:
            raise
        uow.commit()


def latest_run_for(database: Database, submission: Submission) -> GradingRun | None:
    with database.unit_of_work() as uow:
        return uow.runs.latest_for(submission.id)


def regrade_reason(job: GradingJob) -> bool:
    return job.reason is JobReason.REGRADE
