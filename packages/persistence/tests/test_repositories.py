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
    KnowledgeComponent,
    Provenance,
    Role,
    Routing,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
    kc_id_for,
    parse_kc_key,
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
from aijudge_skill import InMemorySkillRepository
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


def a_service(database: Database) -> SubmissionService:
    """時計を止めた提出サービス。

    **既定の実時計を使わない。** ジョブの `available_at` が実時刻で入ると、
    `NOW` を基準に予約するテストは日付が変わった翌日から落ちる（実際に落ちた）。
    """
    return SubmissionService(database.unit_of_work, _store(database), clock=lambda: NOW)


def code(text: str = "int main(void){return 0;}") -> list[IncomingFile]:
    return [IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=text.encode())]


def a_run(run_id: str, submission_id: SubmissionId) -> GradingRun:
    return GradingRun(
        id=GradingRunId(run_id),
        submission_id=submission_id,
        context=GradingContext(
            task_version_id=TASK_VERSION,
            subject_profile="cs_lang_c_intro",
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
        subject_profile="cs_lang_c_intro",
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
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )

    with database.unit_of_work() as uow:
        loaded = uow.submissions.get(result.submission.id)
    assert loaded == result.submission


def test_the_idempotency_key_survives_a_commit(database: Database) -> None:
    """トランザクションを跨いで二重投入を防ぐ。ここが本番の要点。"""
    service = a_service(database)
    kwargs = {
        "tenant_id": TENANT,
        "task_version_id": TASK_VERSION,
        "learner_id": LEARNER,
        "subject_profile": "cs_lang_c_intro",
        "files": code(),
    }
    first = service.accept(**kwargs)
    second = service.accept(**kwargs)

    assert second.deduplicated
    assert second.submission.id == first.submission.id
    with database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 1


def test_the_attempt_counter_survives_a_commit(database: Database) -> None:
    service = a_service(database)
    base = {
        "tenant_id": TENANT,
        "task_version_id": TASK_VERSION,
        "learner_id": LEARNER,
        "subject_profile": "cs_lang_c_intro",
    }
    first = service.accept(**base, files=code("one"))
    second = service.accept(**base, files=code("two"))
    assert (first.submission.attempt, second.submission.attempt) == (1, 2)


def test_a_learner_only_sees_their_own_submissions(database: Database) -> None:
    service = a_service(database)
    other = UserId("usr_" + "9" * 32)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code("mine"),
    )
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=other,
        subject_profile="cs_lang_c_intro",
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


def test_pass_rates_leave_out_a_withheld_total(database: Database) -> None:
    """総合点を保留した採点は正答率に入れない（#406）。

    S6 停止中の暫定の採点は、AI 観点を除いた重みで比例配分した点を
    `score_ratio` に持つ。数えると正答率が実際とずれる。
    """
    version = a_task_version(1)
    with database.unit_of_work() as uow:
        uow.tasks.save_version(version)
        for index, withheld in enumerate((False, True)):
            run = a_run(f"grn_{index:032d}", SubmissionId(f"sub_{index:032d}"))
            run = run.model_copy(
                update={
                    "context": run.context.model_copy(update={"task_version_id": version.id}),
                    "unscored_criteria": (CriterionId("crt_" + "9" * 32),) if withheld else (),
                }
            )
            uow.runs.save(run)
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.tasks.pass_rates((version.id,), threshold=0.5) == {str(version.id): (1, 1)}


# --------------------------------------------------------------------------
# ジョブ
# --------------------------------------------------------------------------


def test_a_job_is_reserved_and_completed(database: Database) -> None:
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
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


def test_position_in_queue(database: Database) -> None:
    from aijudge_core import GradingPhase

    service = a_service(database)
    subs = []
    for i in range(3):
        r = service.accept(
            tenant_id=TENANT,
            task_version_id=TASK_VERSION,
            learner_id=UserId(f"usr_{i:032d}"),
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {i};}}"),
        )
        subs.append(r.submission.id)

    with database.unit_of_work() as uow:
        positions = [uow.jobs.position_in_queue(s, GradingPhase.DETERMINISTIC, NOW) for s in subs]
        assert sorted(positions) == [0, 1, 2]
        # 未知の提出・別段階は None。
        assert (
            uow.jobs.position_in_queue(
                SubmissionId("sub_" + "9" * 32), GradingPhase.DETERMINISTIC, NOW
            )
            is None
        )
        assert uow.jobs.position_in_queue(subs[0], GradingPhase.AI, NOW) is None

        # 1 件を予約すると、その提出は 0、残りは 1 以上（RUNNING が前にいる）。
        taken = uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0)
        assert taken is not None
        running_sub = taken.submission_id
        assert uow.jobs.position_in_queue(running_sub, GradingPhase.DETERMINISTIC, NOW) == 0
        for s in subs:
            if s == running_sub:
                continue
            assert uow.jobs.position_in_queue(s, GradingPhase.DETERMINISTIC, NOW) >= 1


def _a_waiting_job(database: Database, starts_at):
    return a_service(database).accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
        grading_starts_at=starts_at,
    )


@pytest.mark.parametrize(
    ("new_start", "expected"),
    [
        (NOW + timedelta(hours=1), NOW + timedelta(hours=1)),
        (NOW + timedelta(hours=3), NOW + timedelta(hours=3)),
        (NOW - timedelta(hours=1), NOW),
        (None, NOW),
    ],
    ids=["earlier", "later", "already-past", "cleared"],
)
def test_a_waiting_job_follows_a_new_grading_start(database: Database, new_start, expected) -> None:
    """**採点開始を変えたら、変更前の提出も新しい時刻に従う。**"""
    result = _a_waiting_job(database, NOW + timedelta(hours=2))

    with database.unit_of_work() as uow:
        moved = uow.jobs.reschedule_waiting([result.submission.id], new_start, NOW)
        uow.commit()
    assert moved == 1

    with database.unit_of_work() as uow:
        assert uow.jobs.get(result.job.id).available_at == expected


def test_a_job_that_can_already_run_is_not_put_back_to_sleep(database: Database) -> None:
    """**いま取れるジョブは動かさない。** 「いままでの提出を採点」の結果を巻き戻さない。"""
    result = _a_waiting_job(database, None)

    with database.unit_of_work() as uow:
        moved = uow.jobs.reschedule_waiting(
            [result.submission.id], NOW + timedelta(hours=2), NOW + timedelta(seconds=1)
        )
        uow.commit()
    assert moved == 0

    with database.unit_of_work() as uow:
        assert uow.jobs.get(result.job.id).available_at == NOW


def test_a_retry_keeps_its_backoff_when_the_grading_start_moves(database: Database) -> None:
    """**再試行の待ちは採点開始とは別物。** 間隔の制御を崩さない。"""
    result = _a_waiting_job(database, None)
    retry_at = NOW + timedelta(minutes=5)

    with database.unit_of_work() as uow:
        job = uow.jobs.reserve(NOW + timedelta(seconds=1), worker="w1", lease_seconds=60.0)
        uow.jobs.update(job.failed(NOW, "boom", backoff_seconds=300.0))
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.jobs.reschedule_waiting([result.submission.id], None, NOW) == 0
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.jobs.get(result.job.id).available_at == retry_at


def test_a_job_moved_to_another_lane_is_taken_only_from_that_lane(database: Database) -> None:
    """書き起こしの列へ回したジョブは、ふつうの列のワーカーに取られない（#627）。

    列は行の `lane` で絞る。付け替え（`update`）が列を写さないと、回した
    ジョブがふつうの列に残り、書き起こしの列のワーカーには永久に来ない。
    """
    from aijudge_core import GradingPhase
    from aijudge_submission import JobLane

    service = a_service(database)
    subs = [
        service.accept(
            tenant_id=TENANT,
            task_version_id=TASK_VERSION,
            learner_id=UserId(f"usr_{i:032d}"),
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {i};}}"),
        ).submission.id
        for i in range(3)
    ]

    with database.unit_of_work() as uow:
        first = uow.jobs.reserve(NOW, worker="det", lease_seconds=60.0, lane=JobLane.DEFAULT)
        assert first is not None
        uow.jobs.update(first.moved_to(JobLane.TRANSCRIPTION, NOW))
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.jobs.pending_count(lane=JobLane.TRANSCRIPTION) == 1
        assert uow.jobs.pending_count(lane=JobLane.DEFAULT) == 2
        # 回したジョブは自分の列で先頭。ふつうの列の 2 件は前に数えない。
        assert uow.jobs.position_in_queue(first.submission_id, GradingPhase.DETERMINISTIC, NOW) == 0

        default_taken = [
            uow.jobs.reserve(NOW, worker="det", lease_seconds=60.0, lane=JobLane.DEFAULT)
            for _ in range(3)
        ]
        # 同じ時刻に積んだジョブは id の順に出るので、順序ではなく顔ぶれを見る。
        assert {job.submission_id for job in default_taken if job is not None} == {
            s for s in subs if s != first.submission_id
        }
        assert default_taken[-1] is None

        moved = uow.jobs.reserve(NOW, worker="tr1", lease_seconds=60.0, lane=JobLane.TRANSCRIPTION)
        assert moved is not None
        assert moved.id == first.id
        assert moved.lane is JobLane.TRANSCRIPTION
        # 回しただけでは試行に数えない。取った今が 1 回目。
        assert moved.attempts == 1


def test_a_worker_without_a_lane_takes_every_lane(database: Database) -> None:
    """列を指定しないワーカーは列を問わない（ワーカー 1 本の構成・#627）。"""
    from aijudge_submission import JobLane

    service = a_service(database)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    with database.unit_of_work() as uow:
        job = uow.jobs.reserve(NOW, worker="det", lease_seconds=60.0)
        assert job is not None
        uow.jobs.update(job.moved_to(JobLane.TRANSCRIPTION, NOW))
        uow.commit()

    with database.unit_of_work() as uow:
        again = uow.jobs.reserve(NOW, worker="any", lease_seconds=60.0)
        assert again is not None
        assert again.lane is JobLane.TRANSCRIPTION


def test_a_reserved_job_is_not_handed_out_twice(database: Database) -> None:
    service = a_service(database)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
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
    service = a_service(database)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
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


def test_unfinalized_rows_are_not_capped(database: Database) -> None:
    """確定されない行が溜まっても、後ろの提出が候補から落ちない（#403）。

    以前は 500 行で切っており、要レビューや異議の行が 500 を超えると
    新しい提出がいつまでも確定されなかった。
    """
    version = a_task_version(1)
    with database.unit_of_work() as uow:
        uow.tasks.save_version(version)
        uow.commit()
    service = a_service(database)
    count = 510
    for index in range(count):
        accepted = service.accept(
            tenant_id=TENANT,
            task_version_id=version.id,
            learner_id=UserId(f"usr_{index:032d}"),
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {index};}}"),
        )
        with database.unit_of_work() as uow:
            uow.runs.save(a_run(f"grn_{index:032d}", accepted.submission.id))
            uow.commit()

    with database.unit_of_work() as uow:
        assert len(uow.reviews.unfinalized_for_task(TASK_ID)) == count


def test_an_expired_lease_with_no_attempts_left_fails_instead(database: Database) -> None:
    """プロセスごと落とす提出を無限に取り直さない（#398）。"""
    from aijudge_submission import DEFAULT_MAX_ATTEMPTS, LEASE_LOST_ERROR

    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    clock = NOW + timedelta(seconds=1)
    for attempt in range(DEFAULT_MAX_ATTEMPTS):
        with database.unit_of_work() as uow:
            assert uow.jobs.reserve(clock, worker=f"w{attempt}", lease_seconds=60.0) is not None
            uow.commit()
        clock += timedelta(seconds=120)

    with database.unit_of_work() as uow:
        assert uow.jobs.reserve(clock, worker="last", lease_seconds=60.0) is None
        uow.commit()
    with database.unit_of_work() as uow:
        stored = uow.jobs.get(result.job.id)
        assert stored is not None
        assert stored.state is JobState.FAILED
        assert stored.last_error == LEASE_LOST_ERROR
        assert stored.attempts == DEFAULT_MAX_ATTEMPTS
        assert uow.jobs.failed_for([result.submission.id])


def test_lock_reads_the_current_holder(database: Database) -> None:
    """完了を書く前の持ち主確認は、ロックを取った時点の値で行う（#399）。"""
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    with database.unit_of_work() as uow:
        first = uow.jobs.reserve(NOW + timedelta(seconds=1), worker="w1", lease_seconds=60.0)
        uow.commit()
    with database.unit_of_work() as uow:
        uow.jobs.reserve(NOW + timedelta(seconds=120), worker="w2", lease_seconds=60.0)
        uow.commit()
    with database.unit_of_work() as uow:
        current = uow.jobs.lock(result.job.id)
        assert current is not None
        assert current.worker == "w2"
        assert not current.held_by(first)


def test_a_worker_can_be_limited_to_one_subject(database: Database) -> None:
    service = a_service(database)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="math_calculus",
        files=code(),
    )
    with database.unit_of_work() as uow:
        assert (
            uow.jobs.reserve(
                NOW, worker="w1", lease_seconds=60.0, subject_profile="cs_lang_c_intro"
            )
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
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    with database.unit_of_work() as uow:
        events = uow.outbox.unpublished()
    assert len(events) == 1
    assert events[0].submission_id == result.submission.id


def test_a_published_event_is_not_returned_again(database: Database) -> None:
    """購読側の再処理を無限に繰り返さない。"""
    service = a_service(database)
    service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    with database.unit_of_work() as uow:
        events = uow.outbox.unpublished()
        uow.outbox.mark_published([event.event_id for event in events])
        uow.commit()
    with database.unit_of_work() as uow:
        assert uow.outbox.unpublished() == ()


def test_purging_a_video_keeps_the_row_and_survives_a_commit(database: Database) -> None:
    """保存期間の消去は**保管の事実**として残る（ADR 0020）。

    JSON の入れ子を書き換えるので、**コミットを跨いで残ることを確かめる**
    ── その場で辞書を書き換えても SQLAlchemy は変更を検知せず、同じ
    UnitOfWork の中では正しく見えて次のトランザクションで消える。
    """
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    (artifact,) = result.submission.artifacts
    purged_at = NOW + timedelta(days=183)

    with database.unit_of_work() as uow:
        marked = uow.submissions.mark_artifacts_purged(
            [(result.submission.id, artifact.id)], purged_at=purged_at
        )
        uow.commit()
    assert marked == 1

    with database.unit_of_work() as uow:
        loaded = uow.submissions.get(result.submission.id)
    assert loaded is not None
    (after,) = loaded.artifacts
    assert after.purged_at == purged_at
    # 行は残る ── その採点が何を見て付いたかは読めるまま。
    assert after.storage_key == artifact.storage_key
    assert after.content_hash == artifact.content_hash


def test_purging_a_video_twice_keeps_the_first_time(database: Database) -> None:
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    (artifact,) = result.submission.artifacts
    first_time = NOW + timedelta(days=183)
    with database.unit_of_work() as uow:
        uow.submissions.mark_artifacts_purged(
            [(result.submission.id, artifact.id)], purged_at=first_time
        )
        uow.commit()

    with database.unit_of_work() as uow:
        marked = uow.submissions.mark_artifacts_purged(
            [(result.submission.id, artifact.id)], purged_at=NOW + timedelta(days=365)
        )
        uow.commit()

    assert marked == 0
    with database.unit_of_work() as uow:
        loaded = uow.submissions.get(result.submission.id)
    assert loaded is not None
    assert loaded.artifacts[0].purged_at == first_time


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


def test_a_task_keeps_its_own_late_penalty_rule(task_repo) -> None:
    """None（コースに従う）・空（減点しない）・段を、保存して読み戻しても区別できる。"""
    from aijudge_core import LatePenaltyStep

    half = (LatePenaltyStep(after_hours=0.0, ratio=0.5),)
    saved = {
        TASK_ID: half,
        TaskId("tsk_" + "e" * 32): (),
        TaskId("tsk_" + "f" * 32): None,
    }
    with task_repo() as uow:
        for task_id, steps in saved.items():
            uow.tasks.save_task(
                Task(id=task_id, course_id=COURSE, title="t", late_penalty_steps=steps)
            )
        uow.commit()
    with task_repo() as uow:
        for task_id, steps in saved.items():
            assert uow.tasks.get_task(task_id).late_penalty_steps == steps


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


def test_several_versions_are_read_at_once(task_repo) -> None:
    """学習者の画面が、前の版への提出の版を引く（版が上がっても提出は見える）。"""
    with task_repo() as uow:
        for number in (1, 2, 3):
            uow.tasks.save_version(a_task_version(number))
        uow.commit()
    with task_repo() as uow:
        first, third = a_task_version(1).id, a_task_version(3).id
        missing = TaskVersionId("tsv_" + "0" * 32)
        found = uow.tasks.get_versions([first, third, missing, first])
        assert set(found) == {first, third}
        assert found[third].version == 3
        assert uow.tasks.get_versions([]) == {}


def test_all_versions_of_several_tasks_are_read_at_once(task_repo) -> None:
    """配点は版の履歴から決まる。一覧は課題の版をまとめて引く。"""
    with task_repo() as uow:
        for number in (1, 2):
            uow.tasks.save_version(a_task_version(number))
        uow.commit()
    with task_repo() as uow:
        found = uow.tasks.versions_for_tasks([TASK_ID, TaskId("tsk_" + "0" * 32)])
        assert sorted(v.version for v in found) == [1, 2]
        assert uow.tasks.versions_for_tasks([]) == ()


def test_deleting_a_task_takes_its_versions_and_embeddings_only(task_repo) -> None:
    """版に紐づく埋め込みも消える。**他の課題のものは残る。**"""
    other_task = TaskId("tsk_" + "8" * 32)
    other = a_task_version().model_copy(
        update={"id": TaskVersionId("tsv_" + "8" * 32), "task_id": other_task}
    )
    with task_repo() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="消す課題"))
        uow.tasks.save_task(Task(id=other_task, course_id=COURSE, title="残す課題"))
        for version in (a_task_version(1), a_task_version(2), other):
            uow.tasks.save_version(version)
            uow.tasks.save_embedding(
                version.id, model="m", subject_profile="cs_lang_c_intro", vector=(1.0, 0.0)
            )
        uow.commit()
    with task_repo() as uow:
        uow.tasks.delete_task(TASK_ID)
        uow.commit()
    with task_repo() as uow:
        assert uow.tasks.get_task(TASK_ID) is None
        assert uow.tasks.list_versions(TASK_ID) == ()
        assert uow.tasks.get_task(other_task) is not None
        assert uow.tasks.get_version(other.id) == other
        assert set(uow.tasks.list_embeddings(model="m", subject_profile="cs_lang_c_intro")) == {
            str(other.id)
        }


def test_embeddings_do_not_cross_models_or_subjects(task_repo) -> None:
    """次元が同じでも意味空間が違う。混ぜると無関係な課題が似ていることになる。"""
    version = a_task_version()
    with task_repo() as uow:
        uow.tasks.save_version(version)
        uow.tasks.save_embedding(version.id, model="a", subject_profile="s", vector=(1.0, 0.0))
        uow.tasks.save_embedding(version.id, model="b", subject_profile="s", vector=(0.0, 1.0))
        uow.commit()
    with task_repo() as uow:
        assert uow.tasks.list_embeddings(model="a", subject_profile="s") == {
            str(version.id): (1.0, 0.0)
        }
        assert uow.tasks.list_embeddings(model="a", subject_profile="other") == {}


def test_saving_an_embedding_again_replaces_the_vector(task_repo) -> None:
    version = a_task_version()
    with task_repo() as uow:
        uow.tasks.save_version(version)
        uow.tasks.save_embedding(version.id, model="a", subject_profile="s", vector=(1.0, 0.0))
        uow.tasks.save_embedding(version.id, model="a", subject_profile="s", vector=(0.5, 0.5, 0.5))
        uow.commit()
    with task_repo() as uow:
        assert uow.tasks.list_embeddings(model="a", subject_profile="s") == {
            str(version.id): (0.5, 0.5, 0.5)
        }


# --------------------------------------------------------------------------
# KC の語彙 — インメモリと SQL に同じテストを当てる
# --------------------------------------------------------------------------


def a_kc(key: str, **update) -> KnowledgeComponent:
    namespace, path = parse_kc_key(key)
    kc = KnowledgeComponent(id=kc_id_for(key), namespace=namespace, path=path, label=key)
    return kc.model_copy(update=update)


@pytest.fixture(params=["memory", "sql"])
def skill_repo(request, database: Database) -> Callable[[], object]:
    if request.param == "memory":
        repo = InMemorySkillRepository()

        class Holder:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            skills = repo

            def commit(self):
                return None

        return lambda: Holder()
    return database.unit_of_work


def test_kcs_are_listed_by_key_and_narrowed_by_namespace(skill_repo) -> None:
    with skill_repo() as uow:
        for key in ("math.calculus", "cs.recursion", "cs.loops"):
            uow.skills.save_kc(a_kc(key))
        uow.commit()
    with skill_repo() as uow:
        assert [kc.key for kc in uow.skills.list_kcs()] == [
            "cs.loops",
            "cs.recursion",
            "math.calculus",
        ]
        assert [kc.key for kc in uow.skills.list_kcs("cs")] == ["cs.loops", "cs.recursion"]
        found = uow.skills.find_kc_by_key("cs.loops")
        assert found is not None
        assert found.id == kc_id_for("cs.loops")
        assert uow.skills.find_kc_by_key("cs.missing") is None


def test_saving_a_kc_again_updates_it(skill_repo) -> None:
    """名前の変更と退役は同じ ID の上書きで起きる。"""
    with skill_repo() as uow:
        uow.skills.save_kc(a_kc("cs.loops"))
        uow.commit()
    with skill_repo() as uow:
        uow.skills.save_kc(a_kc("cs.loops", label="繰り返し", deprecated=True))
        uow.commit()
    with skill_repo() as uow:
        kc = uow.skills.get_kc(kc_id_for("cs.loops"))
        assert kc is not None
        assert (kc.label, kc.deprecated) == ("繰り返し", True)
        assert len(uow.skills.list_kcs()) == 1


def test_deleting_a_kc_is_quiet_when_it_is_already_gone(skill_repo) -> None:
    with skill_repo() as uow:
        uow.skills.save_kc(a_kc("cs.loops"))
        uow.commit()
    with skill_repo() as uow:
        uow.skills.delete_kc(kc_id_for("cs.loops"))
        uow.skills.delete_kc(kc_id_for("cs.loops"))
        uow.commit()
    with skill_repo() as uow:
        assert uow.skills.get_kc(kc_id_for("cs.loops")) is None


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
    service = a_service(database)
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
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
        # **凍らせた時計で積む。** 実時計で積むと `available_at` が現在時刻に
        # なり、`reserve(NOW, ...)` からは「まだ取れない」ジョブに見える ──
        # NOW を過ぎた日に走らせると 1 件も取れず、取り漏らしとして落ちる。
        # この経路は CI で走っていなかったので、実際に落ちたまま残っていた
        # （#243）。他のテストは `a_service` が同じ時計を渡している。
        service = SubmissionService(database.unit_of_work, store, clock=lambda: NOW)
        job_count = 24
        for index in range(job_count):
            service.accept(
                tenant_id=TENANT,
                task_version_id=TASK_VERSION,
                learner_id=UserId(f"usr_{index:032d}"),
                subject_profile="cs_lang_c_intro",
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


# --------------------------------------------------------------------------
# 左の帯が出す件数（#189）— **方言差が出る場所なのでここに置く**
# --------------------------------------------------------------------------


def test_the_attention_count_excludes_the_instructors_own_trial(database: Database) -> None:
    """教員自身の試行を数えないこと（#108）。

    **このテストがここに居る理由。** 除外の条件は `Submission.is_trial` で、
    これは `submitted_as` から導かれる派生プロパティなので列が無く、JSON の
    中にある。数だけを引くために SQL で JSON を読むので、**SQLite の
    `json_extract` と PostgreSQL の `->>` の両方で同じ答えになる必要がある**
    ── この fixture が両方を回す理由そのもの（冒頭の docstring）。

    片方でしか確かめないと、帯の数字が本番だけずれる。しかも「多めに出る」
    方向にずれるので、教員は減らない件数を見続けることになる。
    """
    version = a_task_version()
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="ex03 p1", session=3))
        uow.tasks.save_version(version)
        uow.commit()

    service = a_service(database)
    submissions = []
    for index, role in enumerate((Role.LEARNER, Role.LEARNER, Role.INSTRUCTOR)):
        result = service.accept(
            tenant_id=TENANT,
            task_version_id=version.id,
            learner_id=UserId(f"usr_{index:032d}"),
            submitted_as=role,
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {index};}}"),
        )
        submissions.append(result.submission.id)
        with database.unit_of_work() as uow:
            uow.runs.save(a_run(f"run_{index:032d}", result.submission.id))
            uow.commit()

    with database.unit_of_work() as uow:
        counts = uow.reviews.attention_counts_for_course(COURSE)

    assert counts.unfinalized == 2, "教員の試行が未確定に混ざっている"
    assert counts.contested == 0
    # 内訳（課題 × 振り分け）は同じ条件で数える ── 足せば合計に一致する（2026-09-25）。
    assert sum(number for _task, _routing, number in counts.unfinalized_by_task) == 2


def test_a_request_closed_by_a_regrade_leaves_the_queue_and_a_new_one_can_be_made(
    database: Database,
) -> None:
    """再採点で終了した依頼は待ち行列にも「異議あり」の件数にも出ない（2026-11-13）。

    新しい採点には改めて依頼できる（依頼は採点 1 件に 1 件）。**方言差が出うる**
    （置き換えを `grading_runs.superseded_by` の列で辿る）ので、両方で回す。
    """
    from datetime import timedelta

    from aijudge_core import ReviewRequest
    from aijudge_core.ids import ReviewRequestId

    version = a_task_version()
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="exam08 p2", session=8))
        uow.tasks.save_version(version)
        uow.commit()
    accepted = a_service(database).accept(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code(),
    )
    submission_id = accepted.submission.id
    old_id, new_id = GradingRunId("grn_" + "a" * 32), GradingRunId("grn_" + "c" * 32)

    def request(run_id: GradingRunId, key: str) -> ReviewRequest:
        return ReviewRequest(
            id=ReviewRequestId("rrq_" + key * 32),
            submission_id=submission_id,
            grading_run_id=run_id,
            learner_id=LEARNER,
            reason="説明が「一部」ですが、5 か所とも手順を書いたつもりです。",
            requested_at=NOW,
        )

    with database.unit_of_work() as uow:
        uow.runs.save(a_run(old_id, submission_id))
        uow.reviews.save_request(request(old_id, "1"))
        uow.commit()
    with database.unit_of_work() as uow:
        assert len(uow.reviews.requested_for_course(COURSE)) == 1
        assert uow.reviews.attention_counts_for_course(COURSE).contested == 1

    with database.unit_of_work() as uow:
        uow.runs.save(
            a_run(new_id, submission_id).model_copy(update={"created_at": NOW + timedelta(hours=1)})
        )
        uow.runs.supersede(old_id, new_id)
        uow.reviews.close_request_for_superseded_run(old_id, new_id, NOW + timedelta(hours=1))
        uow.commit()
    with database.unit_of_work() as uow:
        assert uow.reviews.requested_for_course(COURSE) == ()
        assert uow.reviews.attention_counts_for_course(COURSE).contested == 0
        closed = uow.reviews.request_closed_by(new_id)
        assert closed is not None and closed.id == request(old_id, "1").id
        assert uow.reviews.request_closed_by(old_id) is None

    # 新しい採点への依頼は出せて、待ち行列に出る。
    with database.unit_of_work() as uow:
        uow.reviews.save_request(request(new_id, "2"))
        uow.commit()
    with database.unit_of_work() as uow:
        queued = uow.reviews.requested_for_course(COURSE)
        assert [str(r.id) for _s, _r, r in queued] == [str(request(new_id, "2").id)]
        assert uow.reviews.attention_counts_for_course(COURSE).contested == 1


def test_iterating_a_course_is_not_capped_by_the_listing_limit(database: Database) -> None:
    """**判断に使う経路は打ち切らない**（#219）。

    `list_for_course` の上限は教員の画面のためのもので、古い順に切る。
    コースを消してよいかはそこでは決められない ── 窓の外に学習者の提出が
    あっても「無い」と読めてしまい、提出を成果物ごと消す。

    保存実装とインメモリ実装の両方が同じ Protocol を満たす必要があるので、
    ここは両方に当たる（`database` は SQLite と PostgreSQL を回る）。
    """
    from aijudge_core import Task

    service = a_service(database)
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="例題"))
        uow.tasks.save_version(a_task_version())
        uow.commit()

    # **中身を変える。** 同じ内容の提出は冪等キーで 1 件にまとめられるので
    # （`test_the_same_submission_twice_is_one_submission`）、同じ payload を
    # 3 回出しても 1 件にしかならない。
    for n in range(3):
        service.accept(
            tenant_id=TENANT,
            task_version_id=TaskVersionId(f"tsv_{1:032d}"),
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {n};}}"),
        )

    with database.unit_of_work() as uow:
        capped = uow.submissions.list_for_course(COURSE, limit=2)
        everything = tuple(uow.submissions.iter_for_course(COURSE, chunk=2))

    assert len(capped) == 2, "上限が効いていない（前提が崩れている）"
    assert len(everything) == 3
    # **区切りをまたいでも重複しない・落ちない。**
    assert len({item.id for item in everything}) == 3


def test_the_trial_column_says_what_the_model_says(database: Database) -> None:
    """**列は `Submission.is_trial` の写しであって、定義ではない**（#219）。

    ずれた瞬間に、数える側と数えない側で答えが割れる ── 実際に割れていた
    （JSON を手写しした述語が `is_demo` を落としていて、デモコースへの提出が
    人待ちとして数えられていた）。だからここで**両方が同じことを言う**ことを
    固定する。教員の試行とデモの提出は**別々の経路**で trial になるので、
    どちらも確かめる。
    """
    from sqlalchemy import select

    from aijudge_persistence.schema import SubmissionRow

    service = a_service(database)
    cases = {
        "学習者": {"submitted_as": Role.LEARNER, "is_demo": False},
        "教員の試行": {"submitted_as": Role.INSTRUCTOR, "is_demo": False},
        "デモへの提出": {"submitted_as": Role.LEARNER, "is_demo": True},
    }
    for index, (name, kwargs) in enumerate(cases.items()):
        result = service.accept(
            tenant_id=TENANT,
            task_version_id=TASK_VERSION,
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {index};}}"),
            **kwargs,
        )
        with database.unit_of_work() as uow:
            row = uow._session.execute(  # type: ignore[attr-defined]
                select(SubmissionRow.is_trial).where(SubmissionRow.id == str(result.submission.id))
            ).scalar_one()
        assert bool(row) is result.submission.is_trial, name


def test_counting_a_course_does_not_depend_on_the_listing_limit(database: Database) -> None:
    """数える経路は上限を持たない（#219）。"""
    from aijudge_core import Task

    service = a_service(database)
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="例題"))
        uow.tasks.save_version(a_task_version())
        uow.commit()

    for index in range(3):
        service.accept(
            tenant_id=TENANT,
            task_version_id=TaskVersionId(f"tsv_{1:032d}"),
            learner_id=LEARNER,
            submitted_as=Role.INSTRUCTOR if index == 0 else Role.LEARNER,
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {index};}}"),
        )

    with database.unit_of_work() as uow:
        counts = uow.submissions.count_for_course(COURSE)
        capped = uow.submissions.list_for_course(COURSE, limit=1)

    assert len(capped) == 1, "上限が効いていない（前提が崩れている）"
    assert (counts.learner, counts.trial) == (2, 1)


def test_listing_by_version_is_not_capped(database: Database) -> None:
    """問題セット単位の経路は上限を持たない（#230）。

    **絞り込みが上限の前に来ること**が要点で、逆だとコースが大きいほど
    問題セットの取りこぼしが増える。落ちるのは古い順に切るぶん新しい側で、
    一括採点ではそれが「いま採点したい答案」だった。
    """
    from aijudge_core import Task

    service = a_service(database)
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="例題"))
        uow.tasks.save_version(a_task_version())
        uow.commit()

    version = TaskVersionId(f"tsv_{1:032d}")
    for index in range(3):
        service.accept(
            tenant_id=TENANT,
            task_version_id=version,
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {index};}}"),
        )

    with database.unit_of_work() as uow:
        capped = uow.submissions.list_for_course(COURSE, limit=1)
        by_version = uow.submissions.list_for_versions([version])
        empty = uow.submissions.list_for_versions([])

    assert len(capped) == 1, "上限が効いていない（前提が崩れている）"
    assert len(by_version) == 3
    # **課題版を 1 つも渡さないときに全件を返さない。** 空は「対象なし」で
    # あって「全部」ではない ── ここを取り違えると、問題セットが空のときに
    # コース全体を採点に回す。
    assert empty == ()


def test_the_final_ratio_column_matches_the_domain_function(database: Database) -> None:
    """**列と画面が同じ数でなければならない**（#253）。

    得点の分布は列から数え、一覧は `aijudge_core.final_score` から出す。
    二つが食い違えば図は静かに嘘をつく ── 分布は一覧のように 1 行ずつ
    見比べられないので、食い違いが目で見つからない。

    遅延減点のある採点で確かめる。減点は `score_ratio` に入っていないので、
    **`score_ratio` をそのまま列にした実装はここで落ちる**（この issue の
    最初の案がそれだった）。
    """
    from sqlalchemy import select

    from aijudge_core import LatePenalty, final_score
    from aijudge_persistence.schema import GradingRunRow

    # 採点が指している版そのものを保存する ── 別の版を置いても列は埋まらない
    # （課題版が引けない採点は「数えない」なので NULL になる）。
    version = a_task_version().model_copy(update={"id": TASK_VERSION})
    with database.unit_of_work() as uow:
        uow.tasks.save_version(version)
        uow.commit()

    submission_id = SubmissionId("sub_" + "c" * 32)
    run = a_run("grn_" + "a" * 32, submission_id).model_copy(
        update={
            "penalty": LatePenalty(
                ratio=0.2,
                reason="1 日遅れ",
                hours_late=24.0,
                due_at=NOW,
                submitted_at=NOW + timedelta(days=1),
            )
        }
    )
    with database.unit_of_work() as uow:
        uow.runs.save(run)
        uow.commit()

    expected = final_score(run, version, None).final
    with database.unit_of_work() as uow:
        stored = uow._session.execute(  # type: ignore[attr-defined]
            select(GradingRunRow.final_ratio).where(GradingRunRow.id == str(run.id))
        ).scalar_one()

    assert stored == expected
    # 前提が崩れていないことも言う ── 減点が効いていなければこの試験は
    # 何も確かめていない。
    assert expected != run.score_ratio, "遅延減点が効いていない（前提が崩れている）"


def test_the_scored_read_carries_the_same_number_as_the_listing(database: Database) -> None:
    """**図と一覧は同じ点を見る**（#253）。

    細い読み出しは `final_ratio` を返し、一覧は `final_score` から出す。
    遅延減点のある採点で両者が一致することを言う ── `score_ratio` を返す
    実装はここで落ちる。

    絞り込みも一覧と同じものが効く。
    """
    from aijudge_core import LatePenalty, final_score

    version = a_task_version().model_copy(update={"id": TASK_VERSION})
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="例題"))
        uow.tasks.save_version(version)
        uow.commit()

    service = SubmissionService(database.unit_of_work, _store(database))
    submission = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code("int main(void){return 0;}"),
    ).submission
    run = a_run("grn_" + "a" * 32, submission.id).model_copy(
        update={
            "penalty": LatePenalty(
                ratio=0.2,
                reason="1 日遅れ",
                hours_late=24.0,
                due_at=NOW,
                submitted_at=NOW + timedelta(days=1),
            )
        }
    )
    with database.unit_of_work() as uow:
        uow.runs.save(run)
        uow.commit()

    with database.unit_of_work() as uow:
        scored = uow.submissions.scored_for_course(COURSE)
        nobody = uow.submissions.scored_for_course(COURSE, task_ids=[])

    assert len(scored) == 1
    assert scored[0].final_ratio == final_score(run, version, None).final
    assert scored[0].final_ratio != run.score_ratio, "遅延減点が効いていない（前提が崩れている）"
    assert scored[0].graded is True
    assert scored[0].is_trial is False
    # 空の列は「該当なし」（`list_for_course` と同じ約束）。
    assert nobody == ()


def test_the_listing_filters_before_it_reads(database: Database) -> None:
    """**絞り込みは上限より前に効く**（#247）。

    以前は上限まで読んでから Python で絞っていたので、「この課題だけ」を
    見ても読み込み量はコース全体のままだった ── 上限に当たると、絞り込みは
    **切り落とされた後ろ**を探すことになり、教員には「その課題の古い提出が
    無い」と見える。上限 1 でも条件に合う提出が出ることで、順序が保存層の
    側にあることが分かる。

    **課題で絞る。課題版ではない** ── 提出は出したときの版を指すので、
    最新版だけで絞ると課題を直す前の提出が消える。
    """
    from aijudge_core import Task

    other_task = TaskId("tsk_" + "9" * 32)
    other_version = TaskVersionId(f"tsv_{2:032d}")
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="例題"))
        uow.tasks.save_version(a_task_version())
        uow.tasks.save_task(Task(id=other_task, course_id=COURSE, title="別の例題"))
        uow.tasks.save_version(a_task_version(version=2).model_copy(update={"task_id": other_task}))
        uow.commit()

    service = SubmissionService(database.unit_of_work, _store(database))
    wanted = service.accept(
        tenant_id=TENANT,
        task_version_id=TaskVersionId(f"tsv_{1:032d}"),
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code("int main(void){return 1;}"),
    ).submission
    # 後から出した方が新しいので、絞らなければ上限 1 でこちらが残る。
    service.accept(
        tenant_id=TENANT,
        task_version_id=other_version,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=code("int main(void){return 2;}"),
    )

    with database.unit_of_work() as uow:
        narrowed = uow.submissions.list_for_course(COURSE, limit=1, task_ids=[TASK_ID])
        nobody = uow.submissions.list_for_course(COURSE, task_ids=[])
        by_learner = uow.submissions.list_for_course(COURSE, learner_ids=[LEARNER])
        no_learner = uow.submissions.list_for_course(COURSE, learner_ids=[])

    assert [s.id for s in narrowed] == [wanted.id], "絞り込みが上限の後ろに来ている"
    # **空の列は「該当なし」。** 全件に化けると、条件に誰も当たらない
    # 絞り込みがコース全体の表示になる ── 絞り込みが壊れたようにしか見えない。
    assert nobody == ()
    assert len(by_learner) == 2
    assert no_learner == ()


def test_the_listing_keeps_the_newest_when_it_has_to_choose(database: Database) -> None:
    """**上限に当たったら古い側を落とす**（#233）。

    以前は古い順に切っていたので、落ちるのは最近の提出だった ── 一覧は
    「実際に何が出ているか」を見る場所なので、それは目的と逆である。
    保存実装とインメモリで同じ順序でなければ、画面が backend によって
    別の提出を見せることになる。
    """
    from aijudge_core import Task

    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=COURSE, title="例題"))
        uow.tasks.save_version(a_task_version())
        uow.commit()

    # **時刻をずらす。** 同じ時刻だと並びは id（内容から導く値）で決まり、
    # どれが「新しい」かが偶然で変わる。
    made = []
    for index in range(3):
        at = NOW + timedelta(minutes=index)
        stamped = SubmissionService(database.unit_of_work, _store(database), clock=lambda at=at: at)
        result = stamped.accept(
            tenant_id=TENANT,
            task_version_id=TaskVersionId(f"tsv_{1:032d}"),
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            files=code(f"int main(void){{return {index};}}"),
        )
        made.append(result.submission.id)

    with database.unit_of_work() as uow:
        newest = uow.submissions.list_for_course(COURSE, limit=1)

    assert len(newest) == 1
    assert newest[0].id == made[-1], "いちばん新しい提出が残っていない"


# --------------------------------------------------------------------------
# blind 採点の訂正 — 追記のみ（ADR 0031）
# --------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sql"])
def review_repo(request, database: Database):
    """インメモリと SQL に同じテストを当てる。

    `runs` も持たせる。SQL の `save_review` / `save_finalization` は採点の行が
    無いと拒むので、確認のテストは先に採点を保存する（#687）。
    """
    from aijudge_submission import InMemoryGradingRunRepository, InMemoryReviewRepository

    if request.param == "memory":
        repo = InMemoryReviewRepository()
        run_repo = InMemoryGradingRunRepository()

        class Holder:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            reviews = repo
            runs = run_repo

            def commit(self):
                return None

        return lambda: Holder()
    return database.unit_of_work


def _correction(at: datetime, level: int, *, key: str):
    from aijudge_core import BlindMarkCorrection
    from aijudge_core.ids import BlindMarkCorrectionId, CriterionId, SubmissionId

    criterion = CriterionId("crt_" + "6" * 32)
    return BlindMarkCorrection(
        id=BlindMarkCorrectionId("bmc_" + key * 32),
        submission_id=SubmissionId("sub_" + "5" * 32),
        corrected_by=LEARNER,
        levels={criterion: level},
        previous_levels={criterion: 3},
        reason="押し間違いでした。一部の 50% です。",
        corrected_at=at,
    )


def test_blind_corrections_are_appended_and_read_oldest_first(review_repo) -> None:
    from datetime import timedelta

    from aijudge_core.ids import SubmissionId

    later = _correction(NOW + timedelta(minutes=5), 1, key="b")
    earlier = _correction(NOW, 2, key="a")
    with review_repo() as uow:
        uow.reviews.save_blind_correction(later)
        uow.reviews.save_blind_correction(earlier)
        uow.commit()
    with review_repo() as uow:
        stored = uow.reviews.blind_corrections(SubmissionId("sub_" + "5" * 32))
    assert stored == (earlier, later)


def test_a_blind_correction_cannot_be_rewritten(review_repo) -> None:
    correction = _correction(NOW, 1, key="a")
    with review_repo() as uow:
        uow.reviews.save_blind_correction(correction)
        uow.commit()
    with review_repo() as uow, pytest.raises(ImmutabilityViolation):
        uow.reviews.save_blind_correction(correction.model_copy(update={"levels": {}}))


def _a_request(run: str, key: str = "1"):
    from aijudge_core import ReviewRequest
    from aijudge_core.ids import GradingRunId, ReviewRequestId, SubmissionId

    return ReviewRequest(
        id=ReviewRequestId("rrq_" + key * 32),
        submission_id=SubmissionId("sub_" + "5" * 32),
        grading_run_id=GradingRunId(run),
        learner_id=LEARNER,
        reason="出力の正しさが「一部」ですが、入力例 3 は仕様どおりのはずです。",
        requested_at=NOW,
    )


def test_a_regrade_closes_only_an_open_request(review_repo) -> None:
    """**再採点で終わるのは未対応の依頼だけ**（2026-11-13）。答えた依頼には触れない。"""
    from datetime import timedelta

    from aijudge_core.ids import GradingRunId, HumanReviewId

    old, new = GradingRunId("grn_" + "a" * 32), GradingRunId("grn_" + "c" * 32)
    answered_run, later = GradingRunId("grn_" + "b" * 32), NOW + timedelta(hours=1)
    with review_repo() as uow:
        uow.reviews.save_request(_a_request(old, "1"))
        uow.reviews.save_request(_a_request(answered_run, "2"))
        uow.reviews.resolve_request(
            _a_request(answered_run, "2").id, HumanReviewId("hrv_" + "9" * 32)
        )
        uow.commit()

    with review_repo() as uow:
        closed = uow.reviews.close_request_for_superseded_run(old, new, later)
        untouched = uow.reviews.close_request_for_superseded_run(
            answered_run, GradingRunId("grn_" + "d" * 32), later
        )
        uow.commit()
    assert closed is not None
    assert (closed.closed_by_run, closed.closed_at, closed.resolved_by) == (new, later, None)
    assert untouched is None

    with review_repo() as uow:
        stored = uow.reviews.find_request_for_run(old)
        answered = uow.reviews.find_request_for_run(answered_run)
        # 2 度目は何もしない（追記のみ・P8）。
        again = uow.reviews.close_request_for_superseded_run(old, new, later)
    assert stored == closed
    assert not stored.pending and stored.closed and not stored.resolved
    assert answered is not None and answered.resolved and not answered.closed
    assert again is None


def test_an_observation_is_kept_once_per_task(skill_repo) -> None:
    """観測は学習者 × KC × 課題で 1 件。**同じ課題は置き換える**（ADR 0032）。"""
    from datetime import UTC, datetime

    from aijudge_core import SkillObservation
    from aijudge_core.ids import (
        CriterionScoreId,
        GradingRunId,
        KcId,
        SubmissionId,
        TaskId,
        TenantId,
        UserId,
    )

    def observation(task: str, run: str, ratio: float) -> SkillObservation:
        at = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
        return SkillObservation(
            tenant_id=TenantId("ten_" + "0" * 32),
            learner_id=UserId("usr_" + "1" * 32),
            kc_id=KcId("kc_" + "2" * 32),
            task_id=TaskId("tsk_" + task * 32),
            submission_id=SubmissionId("sub_" + run * 32),
            grading_run_id=GradingRunId("grn_" + run * 32),
            criterion_score_id=CriterionScoreId("cs_" + run * 32),
            score_ratio=ratio,
            exam=task == "e",
            submitted_at=at,
            observed_at=at,
            # 採点の終わった時刻（#678）。文書に載り、読み戻せること。
            graded_at=at if task == "e" else None,
        )

    with skill_repo() as uow:
        uow.skills.save_observation(observation("a", "1", 0.0))
        uow.skills.save_observation(observation("a", "2", 1.0))  # 出し直し
        uow.skills.save_observation(observation("e", "3", 1.0))
        uow.commit()
    with skill_repo() as uow:
        found = uow.skills.list_observations(
            TenantId("ten_" + "0" * 32), UserId("usr_" + "1" * 32), KcId("kc_" + "2" * 32)
        )
    by_task = {str(item.task_id)[4:5]: item for item in found}
    assert set(by_task) == {"a", "e"}
    assert by_task["a"].score_ratio == 1.0
    assert by_task["e"].exam is True
    assert by_task["e"].graded_at == datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert by_task["a"].graded_at is None, "この欄より前の観測は、時刻を持たないまま読める"


def test_history_is_read_per_scope(skill_repo) -> None:
    """推移は範囲（演習だけ・試験だけ・両方）ごとに読む（ADR 0032）。"""
    from datetime import UTC, datetime

    from aijudge_core import MasteryModel, MasteryScope, SkillPoint
    from aijudge_core.ids import KcId, TenantId, UserId

    tenant, learner = TenantId("ten_" + "0" * 32), UserId("usr_" + "1" * 32)
    at = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    with skill_repo() as uow:
        for scope, mastery in ((MasteryScope.ALL, 0.5), (MasteryScope.EXAM, 0.7)):
            uow.skills.append_point(
                SkillPoint(
                    tenant_id=tenant,
                    learner_id=learner,
                    kc_id=KcId("kc_" + "2" * 32),
                    mastery=mastery,
                    observation_count=1,
                    model=MasteryModel.BKT,
                    scope=scope,
                    recorded_at=at,
                )
            )
        uow.commit()
    with skill_repo() as uow:
        assert [p.mastery for p in uow.skills.history(tenant, (learner,))] == [0.5]
        exam = uow.skills.history(tenant, (learner,), scope=MasteryScope.EXAM)
        assert [(p.mastery, p.scope) for p in exam] == [(0.7, MasteryScope.EXAM)]
        assert uow.skills.history(tenant, (learner,), scope=MasteryScope.PRACTICE) == ()


# --------------------------------------------------------------------------
# 待ちの様子（教員コンソールの「提出」に出す・2026-11-13）
# --------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sql"])
def job_queue(request, database: Database):
    """インメモリと SQL に同じテストを当てる（`task_repo` と同じ形）。"""
    from aijudge_submission import InMemoryJobQueue

    if request.param == "memory":
        queue = InMemoryJobQueue()
        yield lambda: queue
        return
    with database.unit_of_work() as uow:
        yield lambda: uow.jobs
        uow.commit()


def _job(n: int, **fields):
    from aijudge_core import GradingPhase
    from aijudge_core.ids import GradingJobId
    from aijudge_submission import GradingJob

    defaults = {
        "id": GradingJobId(f"gjb_{n:032d}"),
        "tenant_id": TENANT,
        "submission_id": SubmissionId(f"sub_{n:032d}"),
        "task_version_id": TASK_VERSION,
        "subject_profile": "cs_lang_c_intro",
        "phase": GradingPhase.DETERMINISTIC,
        "idempotency_key": f"key-{n}",
        "available_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return GradingJob(**(defaults | fields))


def test_the_queue_load_tells_running_ready_and_held_apart(job_queue) -> None:
    """**処理中・待ち・寝かせを分けて数える。** 終わったジョブは数えない。"""
    from aijudge_core import GradingPhase
    from aijudge_submission import JobLane, QueueLoad

    later = NOW + timedelta(hours=2)
    base = GradingRunId("grn_" + "b" * 32)
    jobs = [
        _job(1),  # 決定的・待ち
        _job(2),  # 決定的・待ち
        _job(3, available_at=later),  # 決定的・寝かせ（採点開始待ち）
        _job(
            4,
            state=JobState.RUNNING,
            attempts=1,
            worker="w1",
            lease_expires_at=NOW + timedelta(minutes=1),
        ),  # 決定的・処理中
        _job(5, lane=JobLane.TRANSCRIPTION),  # 書き起こし・待ち
        _job(6, phase=GradingPhase.AI, base_run_id=base),  # AI・待ち
        _job(
            7,
            state=JobState.DONE,
            grading_run_id=GradingRunId("grn_" + "c" * 32),
        ),  # 終わった（数えない）
    ]
    queue = job_queue()
    for job in jobs:
        queue.enqueue(job)

    load = {(row.phase, row.lane): row for row in queue.load(NOW)}

    assert load == {
        (GradingPhase.AI, JobLane.DEFAULT): QueueLoad(
            phase=GradingPhase.AI, lane=JobLane.DEFAULT, ready=1
        ),
        (GradingPhase.DETERMINISTIC, JobLane.DEFAULT): QueueLoad(
            phase=GradingPhase.DETERMINISTIC, lane=JobLane.DEFAULT, running=1, ready=2, held=1
        ),
        (GradingPhase.DETERMINISTIC, JobLane.TRANSCRIPTION): QueueLoad(
            phase=GradingPhase.DETERMINISTIC, lane=JobLane.TRANSCRIPTION, ready=1
        ),
    }
    # 寝かせは時刻が来れば待ちに変わる。
    (det,) = [
        r
        for r in queue.load(later)
        if r.lane is JobLane.DEFAULT and r.phase is GradingPhase.DETERMINISTIC
    ]
    assert (det.ready, det.held) == (3, 0)


def test_an_empty_queue_has_no_load(job_queue) -> None:
    assert job_queue().load(NOW) == ()


def test_course_ids_using_a_profile_follow_task_versions(task_repo) -> None:
    """課題版が指すプロファイルを持つコースを返す（#691）。メモリ実装と永続化実装の両方で。

    コースの既定ではなく**課題版**を見る。テナントでは絞らない（プロファイルのファイルは
    テナントの外にあるので、別テナントのコースの課題が指すものも「使われている」）。
    """
    other_course = CourseId("crs_" + "9" * 32)
    mine, theirs = TASK_ID, TaskId("tsk_" + "9" * 32)
    with task_repo() as uow:
        uow.tasks.save_task(Task(id=mine, course_id=COURSE, title="t"))
        uow.tasks.save_task(Task(id=theirs, course_id=other_course, title="t"))
        base = a_task_version()
        uow.tasks.save_version(base)  # cs_lang_c_intro（TASK_ID）
        uow.tasks.save_version(
            base.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "9" * 32),
                    "task_id": theirs,
                    "subject_profile": "report_ja",
                }
            )
        )
        uow.commit()

    with task_repo() as uow:
        assert uow.tasks.course_ids_using_profile("report_ja") == (other_course,)
        assert uow.tasks.course_ids_using_profile("cs_lang_c_intro") == (COURSE,)
        assert uow.tasks.course_ids_using_profile("nothing") == ()


# --------------------------------------------------------------------------
# 採点待ちを解放する（`release_waiting`・2026-11-14・#680）
# --------------------------------------------------------------------------


def test_releasing_a_waiting_job_makes_it_takeable_and_keeps_the_document(
    database: Database,
) -> None:
    """試験の一括採点（「いままでの提出を採点」）。**列だけでなく文書も更新する。**

    以前の SQL 実装は列（`available_at` / `updated_at`）だけを書き換え、文書は古い時刻の
    まま ── 次の `reserve` が文書由来の値を列へ書き戻して、解放が黙って巻き戻る
    （インメモリ実装はモデルを更新するので、2 つの実装が食い違っていた）。
    """
    result = _a_waiting_job(database, NOW + timedelta(hours=2))
    later = NOW + timedelta(minutes=5)

    with database.unit_of_work() as uow:
        assert uow.jobs.waiting_count([result.submission.id], later) == 1
        released = uow.jobs.release_waiting([result.submission.id], later)
        uow.commit()
    assert released == 1

    with database.unit_of_work() as uow:
        assert uow.jobs.waiting_count([result.submission.id], later) == 0
        job = uow.jobs.get(result.job.id)
        assert job.available_at == later, "文書の available_at が古いまま"
        assert job.updated_at == later
        taken = uow.jobs.reserve(later, worker="w1", lease_seconds=60.0)
        uow.commit()
    assert taken is not None and taken.id == result.job.id


def test_releasing_leaves_running_and_already_takeable_jobs_alone(database: Database) -> None:
    """走っているジョブのリースと、すでに取れるジョブは数えず動かさない。"""
    ready = _a_waiting_job(database, None)

    with database.unit_of_work() as uow:
        assert uow.jobs.release_waiting([ready.submission.id], NOW) == 0
        taken = uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0)
        uow.commit()
    assert taken is not None

    with database.unit_of_work() as uow:
        assert uow.jobs.release_waiting([ready.submission.id], NOW + timedelta(seconds=1)) == 0
        uow.commit()
    with database.unit_of_work() as uow:
        job = uow.jobs.get(ready.job.id)
    assert job.available_at == NOW


def test_releasing_nothing_returns_zero(database: Database) -> None:
    with database.unit_of_work() as uow:
        assert uow.jobs.release_waiting([], NOW) == 0


# 両実装の食い違い（#687）を xfail で示す
# --------------------------------------------------------------------------


def _xfail_on_memory(request, fixture: str, reason: str) -> None:
    """インメモリ側だけが規則を外れているテストに、その側だけ xfail を付ける。

    **strict にする。** インメモリ実装が直った時点で XPASS になって落ち、
    印を外し忘れて規則のテストが「失敗して当然」のまま残ることを防ぐ。
    """
    if request.node.callspec.params.get(fixture) == "memory":
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))


# --------------------------------------------------------------------------
# 一意制約の受け口（#687）— アプリ側の「あれば返す」をすり抜けた場合
# --------------------------------------------------------------------------


def _two_submissions_and_a_claimed_key(database: Database):
    """提出を 2 つ作り、1 つ目に冪等キー `raced-key` を別のトランザクションで登録する。

    同時に来た 2 本の提出は、どちらも `find_by_idempotency_key` で「無い」を
    見る。先に commit した側がキーを持ち、後の側は検査を経ずに登録しに来る。
    """
    service = a_service(database)
    first, second = (
        service.accept(
            tenant_id=TENANT,
            task_version_id=TASK_VERSION,
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            files=code(text),
        ).submission
        for text in ("one", "two")
    )
    with database.unit_of_work() as uow:
        uow.submissions.remember_idempotency_key(TENANT, "raced-key", first.id)
        uow.commit()
    return first, second


def test_the_database_refuses_a_submission_key_claimed_twice(database: Database) -> None:
    """冪等キーの二重登録を最後に止めるのは `submission_keys` の主キーである。

    主キーが無いと、同じ内容の提出が同時に 2 本届いたとき両方が登録され、
    同じ提出が二重に採点される。例外の型は次のテストで見る。
    """
    from sqlalchemy.exc import IntegrityError

    first, second = _two_submissions_and_a_claimed_key(database)
    with (
        database.unit_of_work() as uow,
        pytest.raises((ImmutabilityViolation, IntegrityError)),
    ):
        uow.submissions.remember_idempotency_key(TENANT, "raced-key", second.id)

    with database.unit_of_work() as uow:
        found = uow.submissions.find_by_idempotency_key(TENANT, "raced-key")
    assert found is not None and found.id == first.id


def test_a_submission_key_claimed_concurrently_is_reported_as_such(database: Database) -> None:
    """制約違反は `ImmutabilityViolation` として呼び出し側へ渡す約束である。

    保存層のコメントは「呼び出し側は再読み込みして既存を使う」と書く。
    SQLAlchemy の `IntegrityError` がそのまま出ると、呼び出し側は保存層の
    実装（SQLAlchemy）を知らないと受けられない。
    """
    _, second = _two_submissions_and_a_claimed_key(database)
    with (
        database.unit_of_work() as uow,
        pytest.raises(ImmutabilityViolation, match="claimed concurrently"),
    ):
        uow.submissions.remember_idempotency_key(TENANT, "raced-key", second.id)


def _blind_to_the_first_lookup(monkeypatch) -> None:
    """`SqlJobQueue.find_by_idempotency_key` の最初の 1 回だけ「無い」と答えさせる。

    2 つのワーカーが同時に同じ仕事を積むと、どちらも先の検査で「無い」を見る。
    その状態を 1 スレッドで作るための細工で、2 回目（制約違反の後の読み直し）は
    本物を通す。
    """
    from aijudge_persistence.repositories import SqlJobQueue

    real = SqlJobQueue.find_by_idempotency_key
    calls: list[str] = []

    def blind_once(self, key: str):
        if not calls:
            calls.append(key)
            return None
        return real(self, key)

    monkeypatch.setattr(SqlJobQueue, "find_by_idempotency_key", blind_once)


def test_a_job_enqueued_concurrently_returns_the_one_already_there(
    database: Database, monkeypatch
) -> None:
    """同じ冪等キーのジョブは `uq_jobs_idempotency_key` が止め、既存が返る。

    制約が無い、または受け口が無いと、同じ提出を二度採点するジョブが
    2 行できる（GPU を二度回し、GradingRun が 2 つできる）。
    """
    first = _job(1)
    with database.unit_of_work() as uow:
        uow.jobs.enqueue(first)
        uow.commit()

    rival = _job(2, idempotency_key=first.idempotency_key)
    _blind_to_the_first_lookup(monkeypatch)
    with database.unit_of_work() as uow:
        returned = uow.jobs.enqueue(rival)
        uow.commit()

    assert returned.id == first.id
    with database.unit_of_work() as uow:
        assert uow.jobs.get(rival.id) is None
        assert uow.jobs.find_by_idempotency_key(first.idempotency_key) == first


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#687: SqlJobQueue.enqueue は制約違反でセッションごと rollback するので、"
        "同じトランザクションで先に書いたもの（worker._record_success なら採点結果・"
        "outbox・DONE）が黙って消える"
    ),
)
def test_an_enqueue_collision_keeps_what_the_transaction_wrote_before(
    database: Database, monkeypatch
) -> None:
    """積む前に同じトランザクションで書いたものは、衝突しても残るべきである。

    ワーカーは採点結果の保存・DONE・AI 段階の投入を 1 つのトランザクションで
    行う（`_record_success`）。投入が衝突して rollback すると、そのあとの
    commit は空のまま成功し、採点したのに結果が残らない。
    """
    first = _job(1)
    with database.unit_of_work() as uow:
        uow.jobs.enqueue(first)
        uow.commit()

    run_id = GradingRunId("grn_" + "a" * 32)
    _blind_to_the_first_lookup(monkeypatch)
    with database.unit_of_work() as uow:
        uow.runs.save(a_run(run_id, SubmissionId("sub_" + "b" * 32)))
        uow.jobs.enqueue(_job(2, idempotency_key=first.idempotency_key))
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.runs.get(run_id) is not None


def test_a_second_version_with_the_same_number_is_refused(task_repo, request) -> None:
    """同じ課題の同じ版番号は 1 つだけ（`uq_task_version`）。

    ID だけを見る検査では、ID の違う「版 1」をもう一つ保存できてしまう。
    そうなると `latest_version` やどの版で採点したかが行の並び次第になる。
    """
    _xfail_on_memory(
        request,
        "task_repo",
        "#687: InMemoryTaskRepository.save_version は ID しか見ず、同じ task_id・version で"
        " ID の違う版を受け入れる（SQL は uq_task_version で TaskStoreError）",
    )
    from aijudge_authoring import TaskStoreError

    with task_repo() as uow:
        uow.tasks.save_version(a_task_version(1))
        uow.commit()
    rival = a_task_version(1, statement="別の問題文").model_copy(
        update={"id": TaskVersionId("tsv_" + "f" * 32)}
    )
    with task_repo() as uow, pytest.raises(TaskStoreError, match="already has a version 1"):
        uow.tasks.save_version(rival)


# --------------------------------------------------------------------------
# lock() は同じセッションで先に読んでいても今の値を読む（#399・#687）
# --------------------------------------------------------------------------


def test_lock_rereads_a_job_already_read_in_the_same_session(database: Database) -> None:
    """先に読んだジョブを `lock` し直すと、別のワーカーが取り直した後の値になる。

    `populate_existing` が無いと、セッションの identity map に残った古い行が
    返り、リースを失ったワーカーが「まだ持っている」と判定して結果と DONE を
    書く（#399 の二重採点）。既存の `test_lock_reads_the_current_holder` は
    毎回新しいセッションで読むので、この経路を通らない。

    **行を握っておく。** SQLAlchemy の identity map は弱参照で、`jobs.get` は
    行ではなく `GradingJob` を返すので、読んだ行はすぐ捨てられ、次の `get` は
    読み直しになる。いまの公開の経路では古い行が残らず、`populate_existing` を
    外してもこのままでは落ちない。行を参照し続ける変更（関連や先読み）が
    入った時点で効き始める守りなので、その状態をセッションで直接作る。
    """
    from aijudge_persistence.schema import GradingJobRow

    first = _job(1)
    with database.unit_of_work() as uow:
        uow.jobs.enqueue(first)
        uow.commit()
    with database.unit_of_work() as uow:
        mine = uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0)
        uow.commit()
    assert mine is not None

    with database.unit_of_work() as uow:
        assert uow.jobs.get(first.id).worker == "w1"
        held = uow._session.get(GradingJobRow, str(first.id))
        assert held is not None
        # 別のセッションで、リースの切れた後に w2 が取り直す。
        with database.unit_of_work() as other:
            assert (
                other.jobs.reserve(NOW + timedelta(seconds=120), worker="w2", lease_seconds=60.0)
                is not None
            )
            other.commit()
        current = uow.jobs.lock(first.id)
        assert current is not None
        assert current.worker == "w2"
        assert not current.held_by(mine)
        assert held.document["worker"] == "w2"


# --------------------------------------------------------------------------
# 確認・確定・blind 採点の規則を両実装に当てる（#687）
# --------------------------------------------------------------------------


_REVIEWED_RUN = GradingRunId("grn_" + "a" * 32)


def _a_review(key: str, at: datetime, level: int):
    from aijudge_core import HumanReview
    from aijudge_core.ids import HumanReviewId

    return HumanReview(
        id=HumanReviewId("hrv_" + key * 32),
        grading_run_id=_REVIEWED_RUN,
        grader_id=LEARNER,
        adjusted_levels={CriterionId("crt_" + "6" * 32): level},
        comment="入力例 3 の出力が仕様と違います。",
        reviewed_at=at,
    )


def _with_a_run(review_repo) -> None:
    with review_repo() as uow:
        uow.runs.save(a_run(_REVIEWED_RUN, SubmissionId("sub_" + "b" * 32)))
        uow.commit()


def test_a_review_cannot_be_rewritten(review_repo, request) -> None:
    """同じ ID の確認を保存し直せない。**訂正は別の確認を追記する**（#275・P8）。"""
    _xfail_on_memory(
        request,
        "review_repo",
        "#687: InMemoryReviewRepository.save_review は同じ ID を無条件に上書きする",
    )
    _with_a_run(review_repo)
    review = _a_review("1", NOW, 1)
    with review_repo() as uow:
        uow.reviews.save_review(review)
        uow.commit()
    with review_repo() as uow, pytest.raises(ImmutabilityViolation, match="already exists"):
        uow.reviews.save_review(review.model_copy(update={"adjusted_levels": {}}))


def test_a_review_of_a_missing_run_is_refused(review_repo, request) -> None:
    """無い採点への確認は保存しない。成績のどこにも結びつかない記録になる。"""
    _xfail_on_memory(
        request,
        "review_repo",
        "#687: InMemoryReviewRepository.save_review は採点の有無を確かめない",
    )
    from aijudge_submission import SubmissionStoreError

    with review_repo() as uow, pytest.raises(SubmissionStoreError):
        uow.reviews.save_review(_a_review("1", NOW, 1))


def test_a_later_review_decides_the_grade(review_repo) -> None:
    """訂正の確認を後から保存すると、成績にはそれが使われる（#275）。"""
    _with_a_run(review_repo)
    first, correction = _a_review("1", NOW, 1), _a_review("2", NOW + timedelta(hours=1), 2)
    with review_repo() as uow:
        uow.reviews.save_review(first)
        uow.reviews.save_review(correction)
        uow.commit()
    with review_repo() as uow:
        assert uow.reviews.find_review_for_run(_REVIEWED_RUN) == correction
        assert uow.reviews.decisions_for_runs([_REVIEWED_RUN])[_REVIEWED_RUN].review == correction
        assert uow.reviews.reviews_for_run(_REVIEWED_RUN) == (first, correction)


def test_the_latest_review_wins_by_time_not_by_save_order(review_repo, request) -> None:
    """「最新」は確認した時刻で決める。保存した順ではない（#275）。

    SQL は `reviewed_at desc, id desc` で選ぶ。インメモリが最後に保存したものを
    返すと、古い時刻の確認を後から保存したとき、成績に使う確認が実装で違う。
    """
    _xfail_on_memory(
        request,
        "review_repo",
        "#687: InMemoryReviewRepository.find_review_for_run は最後に保存したものを返し、"
        "reviewed_at を見ない（decisions_for_runs も同じ）",
    )
    _with_a_run(review_repo)
    newer, older = _a_review("2", NOW + timedelta(hours=1), 2), _a_review("1", NOW, 1)
    with review_repo() as uow:
        uow.reviews.save_review(newer)
        uow.reviews.save_review(older)
        uow.commit()
    with review_repo() as uow:
        assert uow.reviews.find_review_for_run(_REVIEWED_RUN) == newer
        assert uow.reviews.decisions_for_runs([_REVIEWED_RUN])[_REVIEWED_RUN].review == newer


def test_a_run_is_finalised_only_once(review_repo) -> None:
    """二度確定できると成績が二つ存在する。やり直しは再採点から（P8）。"""
    from aijudge_core import Finalization, FinalizationSource
    from aijudge_core.ids import FinalizationId

    _with_a_run(review_repo)

    def finalization(key: str) -> Finalization:
        return Finalization(
            id=FinalizationId("fin_" + key * 32),
            grading_run_id=_REVIEWED_RUN,
            source=FinalizationSource.AUTOMATIC,
            justification="採点から所定の時間が経過しました。",
            finalized_at=NOW,
        )

    with review_repo() as uow:
        uow.reviews.save_finalization(finalization("1"))
        uow.commit()
    with review_repo() as uow, pytest.raises(ImmutabilityViolation, match="already finalised"):
        uow.reviews.save_finalization(finalization("2"))
    with review_repo() as uow:
        assert uow.reviews.find_finalization_for_run(_REVIEWED_RUN) == finalization("1")


def test_a_blind_mark_cannot_be_overwritten(review_repo) -> None:
    """blind 採点は 1 提出に 1 つ。AI を見た後の段階で上書きさせない（ADR 0005）。"""
    from aijudge_core import BlindMark

    submission_id = SubmissionId("sub_" + "5" * 32)
    mark = BlindMark(
        submission_id=submission_id,
        grader_id=LEARNER,
        levels={CriterionId("crt_" + "6" * 32): 2},
        marked_at=NOW,
    )
    with review_repo() as uow:
        uow.reviews.save_blind_mark(mark)
        uow.commit()
    with review_repo() as uow, pytest.raises(ImmutabilityViolation, match="blind mark"):
        uow.reviews.save_blind_mark(
            mark.model_copy(update={"levels": {CriterionId("crt_" + "6" * 32): 3}})
        )
    with review_repo() as uow:
        assert uow.reviews.find_blind_mark(submission_id) == mark


# --------------------------------------------------------------------------
# ジョブの取り方・寝かせ・順位を両実装に当てる（#687）
#
# `release_waiting` はここに入れない。document を更新しない食い違いごと
# #680 が扱っている。
# --------------------------------------------------------------------------


def test_jobs_are_reserved_oldest_first(job_queue) -> None:
    """`available_at` → `created_at` → `id` の順に取る。先に出した学習者が先に返る。"""
    queue = job_queue()
    queue.enqueue(_job(3, available_at=NOW - timedelta(minutes=1)))
    queue.enqueue(_job(1, created_at=NOW + timedelta(seconds=1)))
    queue.enqueue(_job(2))
    queue.enqueue(_job(4, available_at=NOW + timedelta(hours=1)))  # まだ取れない

    order = []
    while (job := queue.reserve(NOW, worker="w1", lease_seconds=60.0)) is not None:
        order.append(job.id)
    assert order == [_job(n).id for n in (3, 2, 1)]


def test_waiting_jobs_are_taken_before_lost_leases(job_queue, request) -> None:
    """**待っているジョブを先に、リース切れは後に取る。**

    SQL は QUEUED を全部見てからリース切れを見る。インメモリは両方を混ぜて
    古い順に取る。どちらが正しいにせよ、同じサービス層のテストが実装で
    別の結果になる。
    """
    _xfail_on_memory(
        request,
        "job_queue",
        "#687: InMemoryJobQueue.reserve は QUEUED とリース切れを混ぜて古い順に取る"
        "（SQL は QUEUED を先に取る）",
    )
    queue = job_queue()
    queue.enqueue(
        _job(
            1,
            available_at=NOW - timedelta(hours=1),
            state=JobState.RUNNING,
            attempts=1,
            worker="lost",
            lease_expires_at=NOW - timedelta(minutes=30),
        )
    )
    queue.enqueue(_job(2))

    first = queue.reserve(NOW, worker="w1", lease_seconds=60.0)
    second = queue.reserve(NOW, worker="w1", lease_seconds=60.0)
    assert (first.id, second.id) == (_job(2).id, _job(1).id)


def test_only_a_submission_waiting_for_its_start_is_rescheduled(job_queue) -> None:
    """採点開始の変更で動くのは、提出の採点待ちで寝かせてあるものだけ。

    再試行の待ち（`attempts` が 1 以上）・いま取れるもの・処理中は動かさない。
    """
    later = NOW + timedelta(hours=2)
    queue = job_queue()
    jobs = {
        "waiting": _job(1, available_at=later),
        "retry": _job(2, available_at=later, attempts=1),
        "ready": _job(3),
        "running": _job(
            4,
            state=JobState.RUNNING,
            attempts=1,
            worker="w1",
            lease_expires_at=NOW + timedelta(minutes=1),
        ),
    }
    for job in jobs.values():
        queue.enqueue(job)

    new_start = NOW + timedelta(hours=1)
    ids = [job.submission_id for job in jobs.values()]
    assert queue.waiting_count(ids, NOW) == 2
    assert queue.reschedule_waiting(ids, new_start, NOW) == 1

    assert queue.get(jobs["waiting"].id).available_at == new_start
    for name in ("retry", "ready", "running"):
        assert queue.get(jobs[name].id).available_at == jobs[name].available_at, name


def test_the_position_counts_running_and_earlier_jobs_in_the_same_lane(job_queue) -> None:
    """順位は「処理中 + 自分より前に取られる待ち」。別の段階・別の列は数えない。"""
    from aijudge_core import GradingPhase
    from aijudge_submission import JobLane

    queue = job_queue()
    for job in (
        _job(
            1,
            state=JobState.RUNNING,
            attempts=1,
            worker="w1",
            lease_expires_at=NOW + timedelta(minutes=1),
        ),
        _job(2, available_at=NOW - timedelta(minutes=2)),
        _job(3, available_at=NOW - timedelta(minutes=1)),
        _job(4),  # 自分
        _job(5, created_at=NOW + timedelta(seconds=1)),  # 後ろ
        _job(6, lane=JobLane.TRANSCRIPTION, available_at=NOW - timedelta(hours=1)),
        _job(
            7,
            phase=GradingPhase.AI,
            base_run_id=GradingRunId("grn_" + "b" * 32),
            available_at=NOW - timedelta(hours=1),
        ),
        _job(8, available_at=NOW + timedelta(hours=1)),  # 寝かせ
    ):
        queue.enqueue(job)

    def position(n: int):
        return queue.position_in_queue(
            SubmissionId(f"sub_{n:032d}"), GradingPhase.DETERMINISTIC, NOW
        )

    assert position(1) == 0
    assert position(4) == 3
    assert position(5) == 4
    assert position(6) == 0
    assert position(8) is None
    assert position(99) is None
