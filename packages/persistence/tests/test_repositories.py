"""保存先の実装が、インメモリ実装と同じ規則を守ることを確かめる。

**同じテストを両方の実装に当てる。** 片方だけ通る規則は、移行したときに
初めて破綻する。SQLite で走らせているが、狙いは PostgreSQL。差が出る箇所
（行ロック）は明示的に切り分けてある。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_authoring import InMemoryTaskRepository
from aijudge_authoring.repository import TaskImmutabilityViolation
from aijudge_core import (
    ArtifactKind,
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
)
from aijudge_core.ids import (
    CourseId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_persistence import Database
from aijudge_submission import (
    ImmutabilityViolation,
    IncomingFile,
    InMemoryArtifactStore,
    JobState,
    SubmissionService,
)

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
TASK_ID = TaskId("tsk_" + "2" * 32)
TASK_VERSION = TaskVersionId("tsv_" + "3" * 32)
LEARNER = UserId("usr_" + "4" * 32)
NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Database:
    """SQLite で常に走らせ、PostgreSQL は URL があるときだけ足す。

    SQLite だけで通してしまうと、方言差（JSON 型・行ロック・timezone）に
    気づけない。CI に PostgreSQL を置くまでの暫定。

    **PostgreSQL では毎テストでスキーマを作り直す。** インメモリ SQLite は
    テストごとに別 DB になるが、PostgreSQL は残る。残ると前のテストの
    ジョブや提出が次のテストに見え、失敗の原因が読めなくなる
    （実際にそうなった: 予約したジョブが前のテストのものだった）。
    """
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def code(text: str = "int main(void){return 0;}") -> list[IncomingFile]:
    return [IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=text.encode())]


def a_run(run_id: str, submission_id: SubmissionId) -> GradingRun:
    return GradingRun(
        id=GradingRunId(run_id),
        submission_id=submission_id,
        context=GradingContext(
            task_version_id=TASK_VERSION,
            subject_profile="cs_intro_c",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "5" * 32),
                criterion_id=CriterionId("crt_" + "6" * 32),
                evaluator_result_id=EvaluatorResultId("evr_" + "7" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=3,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="all tests pass",
            ),
        ),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.AUTO,
        created_at=NOW,
    )


def a_task_version(version: int = 1, statement: str = "問題文") -> TaskVersion:
    criterion = RubricCriterion(
        id=CriterionId("crt_" + "6" * 32),
        code="correctness",
        title="正しさ",
        description="テストケースを通るか",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="不可", descriptor="動かない", score_ratio=0.0),
            RubricLevel(level=3, label="良", descriptor="全て通る", score_ratio=1.0),
        ),
    )
    return TaskVersion(
        id=TaskVersionId(f"tsv_{version:032d}"),
        task_id=TASK_ID,
        version=version,
        subject_profile="cs_intro_c",
        statement=statement,
        criteria=(criterion,),
        max_score=100.0,
        provenance=Provenance(authored_by=LEARNER),
        created_at=NOW,
    )


# --------------------------------------------------------------------------
# 提出 — インメモリと同じ振る舞い
# --------------------------------------------------------------------------


def test_a_submission_round_trips(database: Database) -> None:
    service = SubmissionService(database.unit_of_work, _store(database))
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )

    with database.unit_of_work() as uow:
        loaded = uow.submissions.get(result.submission.id)
    assert loaded == result.submission


def test_the_idempotency_key_survives_a_commit(database: Database) -> None:
    """トランザクションを跨いで二重投入を防ぐ。ここが本番の要点。"""
    service = SubmissionService(database.unit_of_work, _store(database))
    kwargs = {
        "tenant_id": TENANT,
        "task_version_id": TASK_VERSION,
        "learner_id": LEARNER,
        "subject_profile": "cs_intro_c",
        "files": code(),
    }
    first = service.accept(**kwargs)
    second = service.accept(**kwargs)

    assert second.deduplicated
    assert second.submission.id == first.submission.id
    with database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 1


def test_the_attempt_counter_survives_a_commit(database: Database) -> None:
    service = SubmissionService(database.unit_of_work, _store(database))
    base = {
        "tenant_id": TENANT,
        "task_version_id": TASK_VERSION,
        "learner_id": LEARNER,
        "subject_profile": "cs_intro_c",
    }
    first = service.accept(**base, files=code("one"))
    second = service.accept(**base, files=code("two"))
    assert (first.submission.attempt, second.submission.attempt) == (1, 2)


def test_a_learner_only_sees_their_own_submissions(database: Database) -> None:
    service = SubmissionService(database.unit_of_work, _store(database))
    other = UserId("usr_" + "9" * 32)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code("mine"),
    )
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=other,
        subject_profile="cs_intro_c",
        files=code("theirs"),
    )
    with database.unit_of_work() as uow:
        mine = uow.submissions.list_for_learner(TENANT, LEARNER)
    assert len(mine) == 1
    assert mine[0].learner_id == LEARNER


def test_nothing_is_written_without_a_commit(database: Database) -> None:
    """commit しないまま抜けたら残らない。中途半端に残さない。"""
    submission_id = SubmissionId("sub_" + "e" * 32)
    with database.unit_of_work() as uow:
        uow.runs.save(a_run("grn_" + "a" * 32, submission_id))
    with database.unit_of_work() as uow:
        assert uow.runs.latest_for(submission_id) is None


# --------------------------------------------------------------------------
# 採点結果は追記のみ（P8）
# --------------------------------------------------------------------------


def test_saving_the_same_run_twice_is_refused(database: Database) -> None:
    submission_id = SubmissionId("sub_" + "b" * 32)
    with database.unit_of_work() as uow:
        uow.runs.save(a_run("grn_" + "a" * 32, submission_id))
        uow.commit()
    with database.unit_of_work() as uow, pytest.raises(ImmutabilityViolation, match="already"):
        uow.runs.save(a_run("grn_" + "a" * 32, submission_id))


def test_regrading_keeps_the_old_run_and_points_it_at_the_new_one(database: Database) -> None:
    """過去の採点は消さない。異議申し立ての根拠になる。"""
    submission_id = SubmissionId("sub_" + "b" * 32)
    old_id, new_id = GradingRunId("grn_" + "a" * 32), GradingRunId("grn_" + "c" * 32)
    with database.unit_of_work() as uow:
        uow.runs.save(a_run(old_id, submission_id))
        uow.runs.save(a_run(new_id, submission_id))
        uow.runs.supersede(old_id, new_id)
        uow.commit()

    with database.unit_of_work() as uow:
        assert len(uow.runs.list_for(submission_id)) == 2
        old = uow.runs.get(old_id)
        assert old is not None
        assert old.superseded_by == new_id


def test_superseding_twice_is_refused(database: Database) -> None:
    submission_id = SubmissionId("sub_" + "b" * 32)
    old_id = GradingRunId("grn_" + "a" * 32)
    with database.unit_of_work() as uow:
        uow.runs.save(a_run(old_id, submission_id))
        uow.runs.supersede(old_id, GradingRunId("grn_" + "c" * 32))
        uow.commit()
    with database.unit_of_work() as uow, pytest.raises(ImmutabilityViolation, match="already"):
        uow.runs.supersede(old_id, GradingRunId("grn_" + "d" * 32))


# --------------------------------------------------------------------------
# ジョブ
# --------------------------------------------------------------------------


def test_a_job_is_reserved_and_completed(database: Database) -> None:
    service = SubmissionService(database.unit_of_work, _store(database))
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )

    with database.unit_of_work() as uow:
        job = uow.jobs.reserve(NOW + timedelta(seconds=1), worker="w1", lease_seconds=60.0)
        assert job is not None
        assert job.id == result.job.id
        assert job.attempts == 1
        uow.jobs.update(job.completed(NOW, GradingRunId("grn_" + "a" * 32)))
        uow.commit()

    with database.unit_of_work() as uow:
        stored = uow.jobs.get(result.job.id)
        assert stored is not None
        assert stored.state is JobState.DONE
        assert uow.jobs.pending_count() == 0


def test_a_reserved_job_is_not_handed_out_twice(database: Database) -> None:
    service = SubmissionService(database.unit_of_work, _store(database))
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )
    later = NOW + timedelta(seconds=1)
    with database.unit_of_work() as uow:
        assert uow.jobs.reserve(later, worker="w1", lease_seconds=600.0) is not None
        uow.commit()
    with database.unit_of_work() as uow:
        assert uow.jobs.reserve(later, worker="w2", lease_seconds=600.0) is None


def test_an_expired_lease_is_handed_to_another_worker(database: Database) -> None:
    """ワーカーが死んだジョブを拾い直す。放置するとその学習者だけ返らない。"""
    service = SubmissionService(database.unit_of_work, _store(database))
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )
    with database.unit_of_work() as uow:
        first = uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0)
        assert first is not None
        uow.commit()

    with database.unit_of_work() as uow:
        second = uow.jobs.reserve(NOW + timedelta(seconds=120), worker="w2", lease_seconds=60.0)
        assert second is not None
        assert second.id == first.id
        assert second.attempts == 2


def test_a_worker_can_be_limited_to_one_subject(database: Database) -> None:
    service = SubmissionService(database.unit_of_work, _store(database))
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="math_calculus",
        files=code(),
    )
    with database.unit_of_work() as uow:
        assert (
            uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0, subject_profile="cs_intro_c")
            is None
        )
        assert (
            uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0, subject_profile="math_calculus")
            is not None
        )


def test_row_locking_is_declared_honestly(database: Database) -> None:
    """SQLite に行ロックは無い。**黙って隠さない。**

    偽の環境で採点ワーカーを複数立てると、同じ提出が二度採点される。
    """
    expected = database.engine.dialect.name != "sqlite"
    assert database.supports_row_locking is expected


# --------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------


def test_the_event_is_stored_with_the_submission(database: Database) -> None:
    """提出とイベントが同時に成立する。片方だけ残らない。"""
    service = SubmissionService(database.unit_of_work, _store(database))
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )
    with database.unit_of_work() as uow:
        events = uow.outbox.unpublished()
    assert len(events) == 1
    assert events[0].submission_id == result.submission.id


def test_a_published_event_is_not_returned_again(database: Database) -> None:
    """購読側の再処理を無限に繰り返さない。"""
    service = SubmissionService(database.unit_of_work, _store(database))
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )
    with database.unit_of_work() as uow:
        events = uow.outbox.unpublished()
        uow.outbox.mark_published([event.event_id for event in events])
        uow.commit()
    with database.unit_of_work() as uow:
        assert uow.outbox.unpublished() == ()


# --------------------------------------------------------------------------
# 課題 — 公開後は不変（P8）
# --------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sql"])
def task_repo(request, database: Database) -> Callable[[], object]:
    """インメモリと SQL に同じテストを当てる。"""
    if request.param == "memory":
        repo = InMemoryTaskRepository()

        class Holder:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            tasks = repo

            def commit(self):
                return None

        return lambda: Holder()
    return database.unit_of_work


def test_a_task_version_round_trips(task_repo) -> None:
    version = a_task_version()
    with task_repo() as uow:
        uow.tasks.save_version(version)
        uow.commit()
    with task_repo() as uow:
        assert uow.tasks.get_version(version.id) == version


def test_reimporting_the_same_version_is_idempotent(task_repo) -> None:
    """決定的 ID で取り込む経路では、同じ版を何度も保存しに来る。"""
    version = a_task_version()
    with task_repo() as uow:
        uow.tasks.save_version(version)
        uow.tasks.save_version(version)
        uow.commit()
    with task_repo() as uow:
        assert uow.tasks.get_version(version.id) == version


def test_changing_a_stored_version_is_refused(task_repo) -> None:
    """問題文の訂正は新しい版を作る。過去の採点基準を書き換えない（P8）。"""
    version = a_task_version()
    with task_repo() as uow:
        uow.tasks.save_version(version)
        uow.commit()
    with task_repo() as uow, pytest.raises(TaskImmutabilityViolation, match="already exists"):
        uow.tasks.save_version(version.model_copy(update={"statement": "書き換えた問題文"}))


def test_the_latest_version_is_the_highest_number(task_repo) -> None:
    with task_repo() as uow:
        uow.tasks.save_version(a_task_version(1))
        uow.tasks.save_version(a_task_version(3))
        uow.tasks.save_version(a_task_version(2))
        uow.commit()
    with task_repo() as uow:
        latest = uow.tasks.latest_version(TASK_ID)
        assert latest is not None
        assert latest.version == 3


def test_tasks_are_listed_per_course(database: Database) -> None:
    task = Task(id=TASK_ID, course_id=COURSE, title="最大値・最小値・平均値")
    other_course = CourseId("crs_" + "9" * 32)
    with database.unit_of_work() as uow:
        uow.tasks.save_task(task)
        uow.tasks.save_task(
            task.model_copy(update={"id": TaskId("tsk_" + "8" * 32), "course_id": other_course})
        )
        uow.commit()
    with database.unit_of_work() as uow:
        assert len(uow.tasks.list_for_course(COURSE)) == 1


def _store(database: Database):
    from aijudge_submission import InMemoryArtifactStore

    if not hasattr(database, "_artifact_store"):
        database._artifact_store = InMemoryArtifactStore()  # type: ignore[attr-defined]
    return database._artifact_store  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 日時 — 締切判定の土台
# --------------------------------------------------------------------------


def test_datetimes_come_back_timezone_aware(database: Database) -> None:
    """バックエンドが tzinfo を落としても、読み出しは aware であること。

    SQLite は落とす。naive な値が混ざると、締切判定がサーバのローカル時刻に
    依存するか、aware な値との比較で TypeError になる。
    """
    service = SubmissionService(database.unit_of_work, _store(database))
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_intro_c",
        files=code(),
    )
    with database.unit_of_work() as uow:
        job = uow.jobs.get(result.job.id)
    assert job is not None
    assert job.available_at.tzinfo is not None
    assert job.created_at.tzinfo is not None


def test_a_naive_datetime_is_refused_on_write(database: Database) -> None:
    """`datetime.now()` を `datetime.now(UTC)` の代わりに使った誤りを落とす。"""
    from sqlalchemy.exc import StatementError

    from aijudge_persistence.schema import OutboxRow

    with database.session() as session:
        session.add(
            OutboxRow(
                event_id="evt_" + "0" * 32,
                tenant_id=str(TENANT),
                type="submission.created",
                occurred_at=datetime(2026, 8, 28, 9, 0),
                published_at=None,
                document={},
            )
        )
        with pytest.raises((ValueError, StatementError), match="naive"):
            session.flush()


# --------------------------------------------------------------------------
# 同時実行 — 行ロックが効いていること
# --------------------------------------------------------------------------


@pytest.mark.skipif(POSTGRES_URL is None, reason="needs PostgreSQL (SQLite has no row locks)")
def test_concurrent_workers_never_take_the_same_job() -> None:
    """複数ワーカーが同じジョブを取らないこと。

    取ると同じ提出が二度採点され、1 つの提出に GradingRun が 2 行できる。
    どちらが成績なのかが決まらない。

    **SQLite では確かめられない**（行ロックが無い）。だからこのテストは
    PostgreSQL でしか走らない。`Database.supports_row_locking` が偽の環境で
    ワーカーを複数立ててはならない、という申告の裏付けがここにある。
    """
    import threading

    from aijudge_persistence import Base

    database = Database.connect(POSTGRES_URL, create=False)
    try:
        Base.metadata.drop_all(database.engine)
        Base.metadata.create_all(database.engine)

        store = InMemoryArtifactStore()
        service = SubmissionService(database.unit_of_work, store)
        job_count = 24
        for index in range(job_count):
            service.accept(
                tenant_id=TENANT,
                task_version_id=TASK_VERSION,
                learner_id=UserId(f"usr_{index:032d}"),
                subject_profile="cs_intro_c",
                files=code(f"int main(void){{return {index};}}"),
            )

        taken: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def drain() -> None:
            # 全スレッドを同時に走らせる。ずらすと競合が起きない。
            barrier.wait()
            while True:
                with database.unit_of_work() as uow:
                    job = uow.jobs.reserve(
                        NOW, worker=f"w{threading.get_ident()}", lease_seconds=600.0
                    )
                    if job is None:
                        uow.commit()
                        return
                    uow.jobs.update(job.completed(NOW, GradingRunId(f"grn_{job.id[4:]}")))
                    uow.commit()
                with lock:
                    taken.append(str(job.id))

        threads = [threading.Thread(target=drain) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(taken) == job_count, f"取り漏らし: {len(taken)}/{job_count}"
        assert len(set(taken)) == job_count, "同じジョブが二度配られた"
    finally:
        database.dispose()
