"""提出から採点まで一巡することを確かめる（Phase 0 の合格基準）。

ここが Phase 0 で唯一「S3 と S5 が実際に噛み合っている」ことを見る場所。
LLM は呼ばず、決定的評価器とスクリプト応答で配線だけを確かめる。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import ArtifactKind, GradingCompleted, Routing, Task
from aijudge_core.events import SubmissionCreated
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_grader import EventRelay, GradingWorker
from aijudge_grading import EvaluatorRegistry
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_persistence import Database, ObservationFileStore
from aijudge_submission import (
    FilesystemArtifactStore,
    IncomingFile,
    JobState,
    SubmissionService,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
EXAMPLE_SOURCE = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "marks" / "s001.c"
PROFILES = REPO_ROOT / "subjects"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
AUTHOR = UserId("usr_" + "3" * 32)
PROFILE = "cs_intro_c"
PROFILE_SAMPLES = 3

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

AI_SAYS_1 = (
    '{"level": 1, "evidence": [{"start_line": 5, "end_line": 5, '
    '"quote": "int b = 0, c = 0, d = 0;"}], '
    '"rationale": "変数名から役割が読み取れません。"}'
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class World:
    """1 テストぶんの環境。DB・ストア・サービス・ワーカーを束ねる。"""

    def __init__(self, tmp_path: Path, responses: list[str] | None = None) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.observations = ObservationFileStore(tmp_path / "observations")
        self.clock = Clock()

        self.provider = ScriptedProvider(
            [r for r in (responses or [AI_SAYS_1]) for _ in range(PROFILE_SAMPLES)]
        )
        registry = EvaluatorRegistry().load_installed()
        registry.replace(RubricAiJudge(LlmGateway(self.provider), model="stub"))

        self.service = SubmissionService(self.database.unit_of_work, self.store, clock=self.clock)
        self.worker = GradingWorker(
            self.database,
            self.store,
            profiles_dir=PROFILES,
            registry=registry,
            observations=self.observations,
            clock=self.clock,
        )
        self.task_version = sharif_judge.import_problem(
            EXAMPLE_TASK,
            subject_profile=PROFILE,
            authored_by=AUTHOR,
            readability_weight=0.3,
        )
        with self.database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=self.task_version.task_id, course_id=COURSE, title="例題"))
            uow.tasks.save_version(self.task_version)
            uow.commit()

    def submit(self, source: bytes | None = None):
        payload = source if source is not None else EXAMPLE_SOURCE.read_bytes()
        return self.service.accept(
            tenant_id=TENANT,
            task_version_id=self.task_version.id,
            learner_id=LEARNER,
            subject_profile=PROFILE,
            files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=payload)],
        )

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# 一巡
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_submission_is_graded_end_to_end(world: World) -> None:
    """提出 → キュー → 採点 → 結果。ここが Phase 0 の骨格。"""
    accepted = world.submit()
    result = world.worker.run_once()

    assert result is not None
    assert result.graded, result.error
    assert result.job.state is JobState.DONE

    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    assert run.id == result.job.grading_run_id
    assert 0.0 <= run.score_ratio <= 1.0


@needs_c_compiler
def test_grading_needs_no_instructor_action(world: World) -> None:
    """教員が何もしていない状態で採点が完了する（ADR 0007）。"""
    world.submit()
    assert world.worker.run_once() is not None


@needs_c_compiler
def test_the_queue_empties(world: World) -> None:
    world.submit()
    graded, errors = world.worker.run_until_empty()
    assert (graded, errors) == (1, ())
    with world.database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 0


def test_an_empty_queue_returns_nothing(world: World) -> None:
    assert world.worker.run_once() is None


@needs_c_compiler
def test_the_grading_completed_event_is_published(world: World) -> None:
    """S5 → S7 / S9 の結合点。購読側が居なくても記録は残る。"""
    world.submit()
    world.worker.run_once()

    with world.database.unit_of_work() as uow:
        events = uow.outbox.unpublished()
    types = [event.type for event in events]
    assert "submission.created" in types
    assert "grading.completed" in types


@needs_c_compiler
def test_the_relay_delivers_events_once(world: World) -> None:
    world.submit()
    world.worker.run_once()

    seen: list[str] = []
    relay = EventRelay(world.database)
    relay.subscribe("submission.created", lambda e: seen.append(e.type))
    relay.subscribe("grading.completed", lambda e: seen.append(e.type))

    assert relay.drain() == 2
    assert sorted(seen) == ["grading.completed", "submission.created"]
    assert relay.drain() == 0, "同じイベントを再送している"


@needs_c_compiler
def test_a_failing_handler_leaves_the_event_for_the_next_pass(world: World) -> None:
    """落ちたイベントを送信済みにすると、そのイベントは永久に失われる。"""
    world.submit()
    relay = EventRelay(world.database)

    calls = {"n": 0}

    def flaky(_event: SubmissionCreated) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("downstream is down")

    relay.subscribe("submission.created", flaky)
    assert relay.drain() == 0
    assert relay.drain() == 1


# --------------------------------------------------------------------------
# 再採点（P8）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_regrading_adds_a_run_and_supersedes_the_old_one(world: World) -> None:
    """過去の採点は消さない。置き換わったことだけを記す。"""
    accepted = world.submit()
    world.worker.run_once()
    world.provider._responses.extend([AI_SAYS_1] * PROFILE_SAMPLES)

    world.service.request_regrade(
        tenant_id=TENANT,
        submission_id=accepted.submission.id,
        subject_profile=PROFILE,
        discriminator="model-v2",
    )
    world.clock.advance(60)
    second = world.worker.run_once()
    assert second is not None and second.graded, second.error if second else None

    with world.database.unit_of_work() as uow:
        runs = uow.runs.list_for(accepted.submission.id)
    assert len(runs) == 2
    assert runs[0].superseded_by == runs[1].id
    assert runs[1].superseded_by is None


# --------------------------------------------------------------------------
# 失敗の扱い
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_missing_subject_profile_fails_permanently(world: World, tmp_path: Path) -> None:
    """人間が設定を直すまで直らない失敗はリトライしない。"""
    world.submit()
    world.worker._profiles_dir = tmp_path / "no-such-dir"

    result = world.worker.run_once()
    assert result is not None
    assert not result.graded
    assert result.job.state is JobState.FAILED
    assert result.job.attempts == 1, "恒久的な失敗でリトライしている"
    assert "科目プロファイルがありません" in (result.error or "")


@needs_c_compiler
def test_a_transient_failure_is_retried(world: World) -> None:
    """一時的な失敗で提出を落とさない。上限まで再試行する。"""
    world.submit()
    original = world.worker._grade
    calls = {"n": 0}

    def flaky(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM timeout")
        return original(job)

    world.worker._grade = flaky  # type: ignore[method-assign]

    first = world.worker.run_once()
    assert first is not None
    assert not first.graded
    assert first.job.state is JobState.QUEUED, "一時的な失敗で打ち切っている"

    # バックオフ中は取れない。
    assert world.worker.run_once() is None
    world.clock.advance(120)
    second = world.worker.run_once()
    assert second is not None and second.graded, second.error if second else None


@needs_c_compiler
def test_a_dead_worker_does_not_strand_a_submission(world: World) -> None:
    """リースが切れたジョブを別のワーカーが引き取る。"""
    world.submit()
    with world.database.unit_of_work() as uow:
        stuck = uow.jobs.reserve(world.clock(), worker="dead", lease_seconds=60.0)
        assert stuck is not None
        uow.commit()

    assert world.worker.run_once() is None, "リース中に奪っている"
    world.clock.advance(120)
    result = world.worker.run_once()
    assert result is not None and result.graded, result.error if result else None


# --------------------------------------------------------------------------
# 観測（測定用の記録）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_grading_writes_observations(world: World) -> None:
    """記録は Phase 0。計算は Phase 1（ADR 0007）。"""
    accepted = world.submit()
    world.worker.run_once()

    stored = world.observations.load(
        PROFILE, str(world.task_version.task_id), str(accepted.submission.id)
    )
    codes = {item.criterion_code for item in stored}
    assert codes == {"correctness", "readability"}
    readability = next(item for item in stored if item.criterion_code == "readability")
    assert readability.machine_level is not None
    assert readability.human_level is None, "教員採点が無いのに入っている"
    assert not readability.usable_for_agreement


@needs_c_compiler
def test_grading_succeeds_even_if_observations_cannot_be_written(world: World) -> None:
    """測定の都合で採点を落とさない（ADR 0007）。"""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    world.observations.save = explode  # type: ignore[method-assign]
    accepted = world.submit()
    result = world.worker.run_once()

    assert result is not None and result.graded, result.error if result else None
    with world.database.unit_of_work() as uow:
        assert uow.runs.latest_for(accepted.submission.id) is not None


@needs_c_compiler
def test_a_worker_without_an_observation_store_still_grades(world: World) -> None:
    """測定用の置き場所を渡さない運用も成立する。"""
    world.worker._observations = None
    world.submit()
    result = world.worker.run_once()
    assert result is not None and result.graded


# --------------------------------------------------------------------------
# 劣化動作（P2）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_grading_completes_without_the_ai_evaluator(world: World, tmp_path: Path) -> None:
    """S6 を止めても決定的評価だけで採点が完結する（設計原則 P2）。"""
    profile = tmp_path / "cs_intro_c.yaml"
    profile.write_text(
        "name: cs_intro_c\n"
        "deterministic: [code_test_runner]\n"
        "ai_evaluators: []\n"
        "timeout_seconds: 60\n",
        encoding="utf-8",
    )
    world.worker._profiles_dir = tmp_path
    world.worker._profiles.clear()

    world.submit()
    result = world.worker.run_once()
    assert result is not None and result.graded, result.error if result else None
    assert result.run is not None
    # 読みやすさは AI 担当なので未採点。暫定の点はレビューへ回る。
    assert result.run.unscored_criteria
    assert result.run.routing is Routing.REVIEW_REQUIRED
    assert world.provider.calls == [], "AI を止めたのに呼ばれている"


@needs_c_compiler
def test_the_result_records_which_grading_run_the_job_produced(world: World) -> None:
    """どの採点結果になったのか辿れる。異議申し立ての根拠になる。"""
    world.submit()
    result = world.worker.run_once()
    assert result is not None and result.run is not None
    assert result.job.grading_run_id == result.run.id


@needs_c_compiler
def test_the_completed_event_carries_the_score(world: World) -> None:
    world.submit()
    world.worker.run_once()
    with world.database.unit_of_work() as uow:
        events = [e for e in uow.outbox.unpublished() if isinstance(e, GradingCompleted)]
    assert len(events) == 1
    assert 0.0 <= events[0].score_ratio <= 1.0


# --------------------------------------------------------------------------
# フィードバック（設計方針 §04 step 6）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_feedback_is_attached_before_the_run_is_saved(tmp_path: Path) -> None:
    """`GradingRun` は保存後不変（P8）なので、保存前に付ける。"""
    from aijudge_feedback import FeedbackGenerator, FeedbackResult

    world = World(tmp_path)
    try:

        class Fixed:
            def generate(self, *_args: object, **_kwargs: object) -> FeedbackResult:
                return FeedbackResult(message="n <= 0 の場合を確かめてください。")

        world.worker._feedback = Fixed()
        accepted = world.submit()
        result = world.worker.run_once()
        assert result is not None and result.graded, result.error if result else None

        with world.database.unit_of_work() as uow:
            run = uow.runs.latest_for(accepted.submission.id)
        assert run is not None
        assert run.feedback == "n <= 0 の場合を確かめてください。"
        assert FeedbackGenerator is not None
    finally:
        world.close()


@needs_c_compiler
def test_a_redundant_fallback_is_not_attached(tmp_path: Path) -> None:
    """観点の説明をそのまま並べただけのものは付けない。

    学習者の画面で同じ文章が 2 度出る（実測で確認）。LLM が使える環境では
    `redundant` が立たないので、そのときは付く。
    """
    from aijudge_feedback import FeedbackGenerator

    world = World(tmp_path)
    try:
        world.worker._feedback = FeedbackGenerator()  # LLM 無し → 要約に落ちる
        accepted = world.submit()
        world.worker.run_once()
        with world.database.unit_of_work() as uow:
            run = uow.runs.latest_for(accepted.submission.id)
        assert run is not None
        assert run.feedback is None
    finally:
        world.close()


@needs_c_compiler
def test_the_feedback_does_not_carry_the_unconfirmed_ai_verdict(tmp_path: Path) -> None:
    """確定前の AI 判定が、フィードバックの文章として漏れないこと。"""
    from aijudge_feedback import FeedbackGenerator

    world = World(tmp_path, responses=[AI_SAYS_1])
    try:
        from aijudge_feedback import summarize_findings

        world.worker._feedback = FeedbackGenerator()
        accepted = world.submit()
        world.worker.run_once()
        with world.database.unit_of_work() as uow:
            run = uow.runs.latest_for(accepted.submission.id)
            task = uow.tasks.get_version(run.context.task_version_id)
        # 要約は付かない（重複するため）が、その材料に AI の判定が
        # 混ざっていないことを確かめる。
        findings = "\n".join(summarize_findings(run, task))
        assert "変数名から役割が読み取れません" not in findings
    finally:
        world.close()


@needs_c_compiler
def test_a_failing_feedback_generator_does_not_fail_the_grading(tmp_path: Path) -> None:
    """フィードバックが出ないことは採点の失敗ではない。"""
    world = World(tmp_path)
    try:

        class Exploding:
            def generate(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("feedback host is down")

        world.worker._feedback = Exploding()
        accepted = world.submit()
        result = world.worker.run_once()

        assert result is not None and result.graded, result.error if result else None
        with world.database.unit_of_work() as uow:
            run = uow.runs.latest_for(accepted.submission.id)
        assert run is not None
        assert run.feedback is None
    finally:
        world.close()
