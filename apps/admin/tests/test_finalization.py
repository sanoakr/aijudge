"""成績の確定の規則を固定する。

固定したいのは 5 つ。

閉じる      依頼が出なかった提出も確定でき、学期末に成績が閉じる。
嘘をつかない  自動確定・一括確定は `HumanReview` を作らない。誰も読んで
            いないものに「教員が同意した」と書けば、そこから測る一致度が
            嘘になる（ADR 0005 / ADR 0010）。
異議を潰さない 未対応の再確認の依頼は自動でも一括でも確定しない。
勝手にやらない  自動確定は `review_required` と未採点の観点を確定させない。
            猶予が未設定のコースでは何も起きない。
冪等        何度走らせても同じ結果になる。二度確定は拒否される。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_admin import finalize_task, pending_counts, sweep_deadlines
from aijudge_admin.finalize_cli import main as finalize_main
from aijudge_admin.operations import AdminError, ensure_course
from aijudge_core import (
    ArtifactKind,
    CriterionScore,
    EvaluatorKind,
    FinalizationSource,
    GradingContext,
    GradingRun,
    Provenance,
    ReviewRequest,
    Routing,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import (
    CourseId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    ReviewRequestId,
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
    SubmissionService,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"

TENANT = TenantId("ten_" + "0" * 32)
TASK_ID = TaskId("tsk_" + "2" * 32)
TASK_VERSION = TaskVersionId("tsv_" + "3" * 32)
CRITERION = CriterionId("crt_" + "6" * 32)
INSTRUCTOR = UserId("usr_" + "9" * 32)

DUE = datetime(2026, 8, 20, 23, 59, tzinfo=UTC)
# 締切 + 24 時間の猶予を過ぎている時刻と、まだ過ぎていない時刻。
AFTER = DUE + timedelta(hours=25)
BEFORE = DUE + timedelta(hours=2)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/f.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    obj, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-前期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    return obj


# -- 世界を組む --------------------------------------------------------------


def _task_version() -> TaskVersion:
    criterion = RubricCriterion(
        id=CRITERION,
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
        id=TASK_VERSION,
        task_id=TASK_ID,
        version=1,
        subject_profile="cs_intro_c",
        statement="問題文",
        criteria=(criterion,),
        max_score=100.0,
        provenance=Provenance(authored_by=INSTRUCTOR),
        created_at=DUE,
    )


def _run(submission_id: SubmissionId, *, routing: Routing, unscored: bool = False) -> GradingRun:
    return GradingRun(
        id=GradingRunId(new_id("grn")),
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
                id=CriterionScoreId(new_id("cs")),
                criterion_id=CRITERION,
                evaluator_result_id=EvaluatorResultId(new_id("evr")),
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
        routing=routing,
        unscored_criteria=(CRITERION,) if unscored else (),
        created_at=DUE,
    )


def _world(
    database: Database,
    course_id: CourseId,
    *,
    due_at: datetime | None = DUE,
    routings: tuple[Routing, ...] = (Routing.AUTO,),
    unscored_at: int | None = None,
    contested_at: int | None = None,
) -> tuple[SubmissionId, ...]:
    """課題 1 件と、指定した振り分けの提出をその数だけ作る。

    `unscored_at` / `contested_at` は「何番目の提出をそうするか」。
    """
    with database.unit_of_work() as uow:
        uow.tasks.save_task(
            Task(id=TASK_ID, course_id=course_id, title="ex03 p1", session=3, due_at=due_at)
        )
        uow.tasks.save_version(_task_version())
        uow.commit()

    service = SubmissionService(database.unit_of_work, InMemoryArtifactStore())
    ids: list[SubmissionId] = []
    for index, routing in enumerate(routings):
        result = service.accept(
            tenant_id=TENANT,
            task_version_id=TASK_VERSION,
            learner_id=UserId(new_id("usr")),
            subject_profile="cs_intro_c",
            files=[
                IncomingFile(
                    filename="main.c",
                    kind=ArtifactKind.CODE,
                    payload=f"int main(void){{return {index};}}".encode(),
                )
            ],
        )
        submission_id = result.submission.id
        ids.append(submission_id)
        run = _run(submission_id, routing=routing, unscored=index == unscored_at)
        with database.unit_of_work() as uow:
            uow.runs.save(run)
            if index == contested_at:
                uow.reviews.save_request(
                    ReviewRequest(
                        id=ReviewRequestId(new_id("rrq")),
                        submission_id=submission_id,
                        grading_run_id=run.id,
                        learner_id=result.submission.learner_id,
                        reason="テストケース 3 の想定出力が仕様と違うと思います。",
                        requested_at=DUE,
                    )
                )
            uow.commit()
    return tuple(ids)


def _finalizations(database: Database, submission_ids: tuple[SubmissionId, ...]):
    out = []
    with database.unit_of_work() as uow:
        for submission_id in submission_ids:
            run = uow.runs.latest_for(submission_id)
            assert run is not None
            out.append(uow.reviews.find_finalization_for_run(run.id))
    return out


# --------------------------------------------------------------------------
# 一括確定 — 教員が課題ごとに閉じる
# --------------------------------------------------------------------------


def test_bulk_finalization_closes_the_submissions_nobody_contested(
    database: Database, course
) -> None:
    """依頼が出なかった提出も確定できる。でないと学期末に成績が閉じない。"""
    ids = _world(database, course.id, routings=(Routing.AUTO,) * 3)

    outcome = finalize_task(
        database,
        task_id=TASK_ID,
        actor_id=INSTRUCTOR,
        justification="テスト全通の提出について、抽出して確認の上まとめて確定します。",
    )

    assert outcome.finalized == 3
    assert outcome.skipped == 0
    for finalization in _finalizations(database, ids):
        assert finalization is not None
        assert finalization.source is FinalizationSource.INSTRUCTOR_BULK


def test_bulk_finalization_writes_no_human_review(database: Database, course) -> None:
    """**一括確定は「教員が読んだ」記録を作らない。**

    作ると、誰も読んでいない提出に「教員が AI の判定に同意した」という
    記録が残る。受講 91 名 × 十数課題では大半がこれになるので、その記録で
    一致度を測れば実力より高い数字が出る（ADR 0005）。
    """
    ids = _world(database, course.id)
    finalize_task(
        database,
        task_id=TASK_ID,
        actor_id=INSTRUCTOR,
        justification="テスト全通の提出をまとめて確定します。",
    )

    with database.unit_of_work() as uow:
        run = uow.runs.latest_for(ids[0])
        assert uow.reviews.find_review_for_run(run.id) is None
        assert uow.reviews.find_finalization_for_run(run.id) is not None


def test_bulk_finalization_includes_what_review_policy_flagged(database: Database, course) -> None:
    """`review_required` も含める。教員が根拠を書いて責任を取る操作だから。

    含めないと、コンパイルエラーや合否境界の提出だけが永久に残る。
    """
    ids = _world(database, course.id, routings=(Routing.REVIEW_REQUIRED,) * 2)
    outcome = finalize_task(
        database,
        task_id=TASK_ID,
        actor_id=INSTRUCTOR,
        justification="コンパイルエラーの提出は 0 点で確定します。再提出は次回に回します。",
    )
    assert outcome.finalized == 2
    assert all(f is not None for f in _finalizations(database, ids))


def test_bulk_finalization_leaves_a_contested_submission_alone(database: Database, course) -> None:
    """未対応の依頼は残す。機械が確定を書き込むと異議申立が無意味になる。"""
    ids = _world(database, course.id, routings=(Routing.AUTO,) * 3, contested_at=1)

    outcome = finalize_task(
        database,
        task_id=TASK_ID,
        actor_id=INSTRUCTOR,
        justification="依頼が出ていない提出をまとめて確定します。",
    )

    assert outcome.finalized == 2
    assert outcome.contested == 1
    finalizations = _finalizations(database, ids)
    assert finalizations[1] is None, "依頼が出ている提出が確定されてしまった"


def test_bulk_finalization_is_idempotent(database: Database, course) -> None:
    """二度目は何もしない。二度確定できると成績が二つ存在する。"""
    _world(database, course.id, routings=(Routing.AUTO,) * 2)
    kwargs = {
        "task_id": TASK_ID,
        "actor_id": INSTRUCTOR,
        "justification": "テスト全通の提出をまとめて確定します。",
    }
    assert finalize_task(database, **kwargs).finalized == 2
    assert finalize_task(database, **kwargs).finalized == 0


def test_finalizing_an_unknown_task_is_refused(database: Database, course) -> None:
    with pytest.raises(AdminError):
        finalize_task(
            database,
            task_id=TaskId("tsk_" + "f" * 32),
            actor_id=INSTRUCTOR,
            justification="存在しない課題を確定しようとしています。",
        )


def test_a_run_cannot_be_finalized_twice(database: Database, course) -> None:
    """保存の側でも拒否する。上位の判定が漏れても成績は二つにならない。"""
    from aijudge_core import Finalization
    from aijudge_core.ids import FinalizationId

    ids = _world(database, course.id)
    finalize_task(
        database,
        task_id=TASK_ID,
        actor_id=INSTRUCTOR,
        justification="テスト全通の提出をまとめて確定します。",
    )
    with database.unit_of_work() as uow:
        run = uow.runs.latest_for(ids[0])
        with pytest.raises(ImmutabilityViolation):
            uow.reviews.save_finalization(
                Finalization(
                    id=FinalizationId(new_id("fin")),
                    grading_run_id=run.id,
                    source=FinalizationSource.INSTRUCTOR_BULK,
                    actor_id=INSTRUCTOR,
                    justification="二度目の確定を試みています。",
                    finalized_at=AFTER,
                )
            )


# --------------------------------------------------------------------------
# 自動確定 — 締切からの猶予
# --------------------------------------------------------------------------


def _with_grace(database: Database, course, hours: float | None):
    """科目の既定の猶予を入れる。**猶予は分**なので時間から換算する。"""
    minutes = None if hours is None else int(hours * 60)
    with database.unit_of_work() as uow:
        uow.identity.save_course(course.model_copy(update={"auto_finalize_after_minutes": minutes}))
        uow.commit()


def test_nothing_happens_when_the_course_declares_no_grace(database: Database, course) -> None:
    """既定は「自動確定しない」。設定漏れではなく設定内容である。"""
    ids = _world(database, course.id)
    report = sweep_deadlines(database, now=AFTER)
    assert report.finalized == 0
    assert _finalizations(database, ids) == [None]


def test_nothing_happens_before_the_grace_has_elapsed(database: Database, course) -> None:
    ids = _world(database, course.id)
    _with_grace(database, course, 24.0)
    assert sweep_deadlines(database, now=BEFORE).finalized == 0
    assert _finalizations(database, ids) == [None]


def test_the_grace_elapsing_finalizes_the_automatic_verdicts(database: Database, course) -> None:
    ids = _world(database, course.id, routings=(Routing.AUTO,) * 3)
    _with_grace(database, course, 24.0)

    report = sweep_deadlines(database, now=AFTER)

    assert report.finalized == 3
    for finalization in _finalizations(database, ids):
        assert finalization is not None
        assert finalization.source is FinalizationSource.AUTOMATIC
        # 人が関与していないことを記録の側で示す。
        assert finalization.actor_id is None
        assert finalization.review_id is None
        assert not finalization.reviewed_individually


def test_automatic_finalization_writes_no_human_review(database: Database, course) -> None:
    ids = _world(database, course.id)
    _with_grace(database, course, 24.0)
    sweep_deadlines(database, now=AFTER)
    with database.unit_of_work() as uow:
        run = uow.runs.latest_for(ids[0])
        assert uow.reviews.find_review_for_run(run.id) is None


def test_automatic_finalization_skips_what_needs_a_human(database: Database, course) -> None:
    """レビュー方針が人の目を求めたものを、人を通さずに成績にしない（P5）。"""
    ids = _world(
        database,
        course.id,
        routings=(Routing.AUTO, Routing.REVIEW_REQUIRED, Routing.AUTO),
    )
    _with_grace(database, course, 24.0)

    report = sweep_deadlines(database, now=AFTER)

    assert report.finalized == 2
    outcome = report.touched[0]
    assert outcome.needs_review == 1
    assert _finalizations(database, ids)[1] is None


def test_automatic_finalization_skips_an_unscored_criterion(database: Database, course) -> None:
    """誰も見ていない観点が成績に入るのを防ぐ。見送りの理由も分けて数える。"""
    ids = _world(
        database,
        course.id,
        routings=(Routing.AUTO, Routing.REVIEW_REQUIRED),
        unscored_at=1,
    )
    _with_grace(database, course, 24.0)

    report = sweep_deadlines(database, now=AFTER)

    assert report.finalized == 1
    outcome = report.touched[0]
    assert (outcome.provisional, outcome.needs_review) == (1, 0)
    assert _finalizations(database, ids)[1] is None


def test_automatic_finalization_leaves_a_contested_submission_alone(
    database: Database, course
) -> None:
    ids = _world(database, course.id, routings=(Routing.AUTO,) * 2, contested_at=0)
    _with_grace(database, course, 24.0)

    report = sweep_deadlines(database, now=AFTER)

    assert (report.finalized, report.skipped) == (1, 1)
    assert _finalizations(database, ids)[0] is None


def test_a_task_without_a_deadline_is_still_finalized(database: Database, course) -> None:
    """**締切が無くても確定する。** 数えるのは採点が終わってからなので。

    以前は締切を起点にしていたため、締切の無い課題は確定しようがなく、
    未確定のまま学期末まで残った。採点完了は必ずあるので、その問題は消える。
    """
    ids = _world(database, course.id, due_at=None)
    _with_grace(database, course, 24.0)
    assert sweep_deadlines(database, now=AFTER).finalized == 1
    assert _finalizations(database, ids) != [None]


def test_a_grade_is_not_finalized_before_its_own_grace_elapses(database: Database, course) -> None:
    """**提出ごとに数える。** 同じ課題でも、採点が遅かった提出はまだ確定しない。"""
    ids = _world(database, course.id)
    _with_grace(database, course, 24.0)
    # 採点は DUE。猶予 24 時間なので DUE+2h ではまだ明けていない。
    report = sweep_deadlines(database, now=BEFORE)
    assert report.finalized == 0
    assert sum(outcome.not_due for outcome in report.outcomes) == 1
    # **見送りではなく待ち。** 混ぜると運用者に「積み上がっている」と見える。
    assert sum(outcome.skipped for outcome in report.outcomes) == 0
    assert _finalizations(database, ids) == [None]


def test_the_sweep_is_idempotent(database: Database, course) -> None:
    """cron で毎日走る。二度目に何かが起きてはいけない。"""
    _world(database, course.id, routings=(Routing.AUTO,) * 2)
    _with_grace(database, course, 24.0)
    assert sweep_deadlines(database, now=AFTER).finalized == 2
    assert sweep_deadlines(database, now=AFTER).finalized == 0


def test_a_dry_run_changes_nothing(database: Database, course) -> None:
    ids = _world(database, course.id)
    _with_grace(database, course, 24.0)

    report = sweep_deadlines(database, now=AFTER, dry_run=True)

    assert report.finalized == 1, "何が確定するかは報告する"
    assert _finalizations(database, ids) == [None], "報告しただけで保存してはいけない"


# --------------------------------------------------------------------------
# 未確定の件数 — 仕掛け忘れに気づくため
# --------------------------------------------------------------------------


def test_the_pending_count_is_visible_and_drops_on_finalization(database: Database, course) -> None:
    """設定したつもりで cron を忘れても、件数が減らないことで気づける。"""
    _world(database, course.id, routings=(Routing.AUTO,) * 3)
    assert pending_counts(database, course.id)[TASK_ID] == 3

    finalize_task(
        database,
        task_id=TASK_ID,
        actor_id=INSTRUCTOR,
        justification="テスト全通の提出をまとめて確定します。",
    )
    assert pending_counts(database, course.id)[TASK_ID] == 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_the_cli_finalizes_once(database: Database, course, tmp_path: Path) -> None:
    ids = _world(database, course.id)
    _with_grace(database, course, 24.0)

    code = finalize_main(
        [
            "--database-url",
            f"sqlite+pysqlite:///{tmp_path}/f.db",
            "--once",
            "--now",
            AFTER.isoformat(),
        ]
    )

    assert code == 0
    assert _finalizations(database, ids)[0] is not None


def test_the_cli_refuses_a_fixed_now_while_resident(database: Database, tmp_path: Path) -> None:
    """常駐で固定時刻を使うと、同じ判定を延々と繰り返すだけになる。"""
    code = finalize_main(
        [
            "--database-url",
            f"sqlite+pysqlite:///{tmp_path}/f.db",
            "--now",
            AFTER.isoformat(),
        ]
    )
    assert code == 2


def test_the_cli_refuses_a_malformed_now(database: Database, tmp_path: Path) -> None:
    code = finalize_main(
        ["--database-url", f"sqlite+pysqlite:///{tmp_path}/f.db", "--once", "--now", "昨日"]
    )
    assert code == 2
