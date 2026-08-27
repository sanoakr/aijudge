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
from aijudge_core import GradingRun, Submission, TaskVersion
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

    # -- 1 件処理 ----------------------------------------------------------

    def run_once(self, *, subject_profile: str | None = None) -> WorkResult | None:
        """ジョブを 1 つ処理する。無ければ None。"""
        now = self._clock()

        # --- 予約。ここで commit してリースを他のワーカーに見せる。 -------
        with self._database.unit_of_work() as uow:
            job = uow.jobs.reserve(
                now,
                worker=self._worker,
                lease_seconds=self._lease_seconds,
                subject_profile=subject_profile,
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
        self, *, subject_profile: str | None = None, limit: int = 1000
    ) -> tuple[int, tuple[str, ...]]:
        """キューが空になるまで処理する。

        1 件の失敗で止めない。締切前に 1 件の異常提出で全員の採点が
        止まるのは受け入れられない。
        """
        graded = 0
        errors: list[str] = []
        for _ in range(limit):
            result = self.run_once(subject_profile=subject_profile)
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

        profile = self._profile(job.subject_profile)
        contents = gradable_contents(submission, self._store)
        pipeline = GradingPipeline(self._registry, profile)
        run = pipeline.run(task_version, submission, lambda artifact: contents[artifact.id])
        return self._with_feedback(run, task_version, contents)

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
            uow.commit()

        # 観測は採点の外側。失敗しても採点は成立させる（ADR 0007）。
        self._record_observations(job, run)
        return done

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
