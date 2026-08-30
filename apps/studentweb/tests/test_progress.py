"""課題ごとの到達状況 ── 回数・採用される点・状態を固定する。

固定したいのは 3 つ。

積み上げ  提出回数と、採用される最大得点。一覧に出さないと、学習者は
          自分の到達点を知るのに課題を 1 つずつ開くことになる。
保留      採点できなかった観点がある提出は、最大値の候補にしない。
          点が出ていないものを採用として示すと「0 点が採用された」に見える。
出所      確定は「教員が確認」「一括」「自動」を区別する（ADR 0010）。
          まとめると、誰も読んでいない自動確定が「教員が確認しました」になる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Course,
    CriterionScore,
    EvaluatorKind,
    Evidence,
    Finalization,
    FinalizationSource,
    GradingContext,
    GradingRun,
    LineSpan,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    Task,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import (
    ArtifactId,
    CourseId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    FinalizationId,
    GradingRunId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_studentweb.progress import load_progress
from aijudge_submission import in_memory_backend

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
TASK = TaskId("tsk_" + "3" * 32)
VERSION = TaskVersionId("tsv_" + "4" * 32)
CORRECTNESS = CriterionId("crt_" + "5" * 32)
READABILITY = CriterionId("crt_" + "6" * 32)


def _levels() -> tuple[RubricLevel, ...]:
    return (
        RubricLevel(level=0, label="不可", descriptor="満たさない", score_ratio=0.0),
        RubricLevel(level=3, label="達成", descriptor="満たす", score_ratio=1.0),
    )


@pytest.fixture
def version() -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TASK,
        version=1,
        subject_profile="cs_intro_c",
        statement="問題文",
        criteria=(
            RubricCriterion(
                id=CORRECTNESS,
                code="correctness",
                title="出力の正しさ",
                description="テスト実行で判定する",
                weight=0.7,
                levels=_levels(),
            ),
            RubricCriterion(
                id=READABILITY,
                code="readability",
                title="読みやすさ",
                description="AI が判定する",
                weight=0.3,
                levels=_levels(),
            ),
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "9" * 32)),
        created_at=NOW,
    )


@pytest.fixture
def course() -> Course:
    return Course(
        id=COURSE,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-前期",
        subject_profile="cs_intro_c",
    )


@pytest.fixture
def task() -> Task:
    return Task(id=TASK, course_id=COURSE, title="例題")


class World:
    """インメモリの保存先に提出と採点を並べる。"""

    def __init__(self) -> None:
        # インメモリの UnitOfWork は 1 つを使い回す（`with` は境界の形だけ）。
        self.uow, _store = in_memory_backend()

    def submit(self, attempt: int, *, at: datetime) -> Submission:
        submission_id = SubmissionId(new_id("sub"))
        submission = Submission(
            id=submission_id,
            task_version_id=VERSION,
            learner_id=LEARNER,
            state=SubmissionState.SUBMITTED,
            attempt=attempt,
            artifacts=(
                Artifact(
                    id=ArtifactId(new_id("art")),
                    submission_id=submission_id,
                    role=ArtifactRole.ORIGINAL,
                    kind=ArtifactKind.CODE,
                    filename="main.c",
                    storage_key=f"{TENANT}/{submission_id}/main.c",
                    content_hash=f"sha256:{attempt}",
                    byte_size=10,
                    created_at=at,
                ),
            ),
            created_at=at,
            submitted_at=at,
        )
        with self.uow as uow:
            uow.submissions.save(submission)
            uow.submissions.remember_idempotency_key(TENANT, f"key-{attempt}", submission_id)
            uow.commit()
        return submission

    def grade(self, submission: Submission, *, ratio: float, complete: bool = True) -> GradingRun:
        """採点を 1 件置く。`complete=False` なら AI 観点が採点できなかった採点。"""
        scores = [
            CriterionScore(
                id=CriterionScoreId(new_id("cs")),
                criterion_id=CORRECTNESS,
                evaluator_result_id=EvaluatorResultId(new_id("evr")),
                kind=EvaluatorKind.DETERMINISTIC,
                level=3 if ratio > 0 else 0,
                score_ratio=ratio,
                weight=1.0 if not complete else 0.7,
                confidence=1.0,
                conclusive=True,
                rationale="テストケースの結果です。",
            )
        ]
        if complete:
            scores.append(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=READABILITY,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.AI,
                    level=3 if ratio > 0 else 0,
                    score_ratio=ratio,
                    weight=0.3,
                    confidence=0.9,
                    conclusive=False,
                    rationale="変数名から役割が読み取れます。",
                    # AI の判定には根拠が要る（core の検証が強制する）。
                    evidence=(
                        Evidence(
                            artifact_id=submission.artifacts[0].id,
                            artifact_content_hash=submission.artifacts[0].content_hash,
                            span=LineSpan(start_line=5, end_line=5),
                            quote="int total = 0;",
                        ),
                    ),
                )
            )
        run = GradingRun(
            id=GradingRunId(new_id("grn")),
            submission_id=submission.id,
            context=GradingContext(
                task_version_id=VERSION,
                subject_profile="cs_intro_c",
                rubric_version="v1",
                input_hash=f"sha256:{submission.attempt}",
                pipeline_version="0.1.0",
            ),
            criterion_scores=tuple(scores),
            score_ratio=ratio,
            confidence=1.0,
            routing=Routing.AUTO if complete else Routing.REVIEW_REQUIRED,
            unscored_criteria=() if complete else (READABILITY,),
            created_at=submission.submitted_at or NOW,
        )
        with self.uow as uow:
            uow.runs.save(run)
            uow.commit()
        return run

    def finalize(self, run: GradingRun, source: FinalizationSource) -> None:
        with self.uow as uow:
            uow.reviews.save_finalization(
                Finalization(
                    id=FinalizationId(new_id("fin")),
                    grading_run_id=run.id,
                    source=source,
                    justification="締切から所定の時間が経過しました。",
                    finalized_at=NOW,
                )
            )
            uow.commit()

    def progress(self, course: Course, task: Task, version: TaskVersion):
        with self.uow as uow:
            loaded = load_progress(
                uow,
                tenant_id=TENANT,
                learner_id=LEARNER,
                course=course,
                rows=((task, version),),
                now=NOW,
            )
        return loaded.get(version.id)


def test_the_attempt_count_and_the_best_score_are_available(course, task, version) -> None:
    """一覧に出す値。回数と、採用される最大得点。"""
    world = World()
    first = world.submit(1, at=NOW - timedelta(hours=3))
    second = world.submit(2, at=NOW - timedelta(hours=2))
    world.grade(first, ratio=0.4)
    world.grade(second, ratio=0.9)

    progress = world.progress(course, task, version)
    assert progress is not None
    assert progress.count == 2
    assert progress.best_ratio == pytest.approx(0.9)


def test_the_best_attempt_is_adopted_even_when_it_is_not_the_last(course, task, version) -> None:
    """最後の提出が最高とは限らない。試しに壊した提出が最後になることがある。"""
    world = World()
    good = world.submit(1, at=NOW - timedelta(hours=3))
    broken = world.submit(2, at=NOW - timedelta(hours=2))
    world.grade(good, ratio=1.0)
    world.grade(broken, ratio=0.2)

    progress = world.progress(course, task, version)
    assert progress.best_ratio == pytest.approx(1.0)
    adopted = progress.adopted
    assert adopted is not None
    assert adopted.submission.id == good.id


def test_each_attempt_carries_its_time_score_and_state(course, task, version) -> None:
    """課題ページの 1 行が持つもの ── 提出日時・得点・状態。"""
    world = World()
    submission = world.submit(1, at=NOW - timedelta(hours=2))
    world.grade(submission, ratio=0.8)

    progress = world.progress(course, task, version)
    (attempt,) = progress.attempts
    assert attempt.submission.submitted_at == NOW - timedelta(hours=2)
    assert attempt.score_ratio == pytest.approx(0.8)
    assert attempt.status_label == "確定前（AI の判定）"


def test_a_withheld_score_is_not_adopted(course, task, version) -> None:
    """採点できなかった観点がある提出は候補にしない。

    点が出ていないものを採用として示すと「0 点が採用された」に見える。
    """
    world = World()
    scored = world.submit(1, at=NOW - timedelta(hours=3))
    partial = world.submit(2, at=NOW - timedelta(hours=2))
    world.grade(scored, ratio=0.5)
    world.grade(partial, ratio=1.0, complete=False)

    progress = world.progress(course, task, version)
    assert progress.withheld
    assert progress.best_ratio == pytest.approx(0.5)
    assert progress.adopted.submission.id == scored.id
    assert progress.attempts[1].status_label == "保留"


def test_an_ungraded_attempt_is_counted_but_shows_as_grading(course, task, version) -> None:
    """採点が届いていない提出も回数には入る。点は出さない。"""
    world = World()
    world.submit(1, at=NOW - timedelta(minutes=5))

    progress = world.progress(course, task, version)
    assert progress.count == 1
    assert progress.grading
    assert progress.best_ratio is None
    assert progress.attempts[0].status_label == "採点中"


def test_an_automatic_finalisation_is_not_shown_as_an_instructor_check(
    course, task, version
) -> None:
    """確定の出所を区別する。まとめると、誰も読んでいない成績が

    「教員が確認しました」として一覧に並ぶ（ADR 0010）。
    """
    world = World()
    submission = world.submit(1, at=NOW - timedelta(hours=2))
    run = world.grade(submission, ratio=0.7)
    world.finalize(run, FinalizationSource.DEADLINE_ELAPSED)

    progress = world.progress(course, task, version)
    assert progress.best_confirmed
    assert progress.attempts[0].status_label == "確定（自動）"


def test_a_task_with_no_submissions_has_no_progress(course, task, version) -> None:
    """未提出の課題は結果に現れない。一覧側は空として扱う。"""
    world = World()
    assert world.progress(course, task, version) is None
