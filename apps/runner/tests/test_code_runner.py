"""runner が試しの実行を採点と同じ条件で動かし、採点とは混ざらないこと（ADR 0024）。

sandbox は偽物に差し替える。確かめたいのは「何を・どの上限で・どの入力で
動かすか」の決め方と、結果の書き戻し方で、コンテナの隔離そのものは
`packages/sandbox/tests` が見ている。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import ArtifactKind, Course, Role, Task
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_eval_code_test_runner import _limits as grading_limits
from aijudge_ide import (
    DISPLAY_OUTPUT_CHARS,
    RUNNER_LOST,
    RunRequest,
    RunStage,
    RunState,
    request_run,
)
from aijudge_persistence import Database
from aijudge_runner import CodeRunner, limits_for
from aijudge_runner.runner import NO_SUCH_SAMPLE, NOT_RUNNABLE, SANDBOX_DOWN
from aijudge_sandbox import ExecRequest, ExecResult, Isolation, SandboxUnavailable
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"
PROFILES = REPO_ROOT / "subjects"
PROFILE = "cs_lang_c_intro"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
AUTHOR = UserId("usr_" + "3" * 32)
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
SOURCE = '#include <stdio.h>\nint main(void){puts("hi");return 0;}\n'


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeWorkspace:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox
        self.path = Path("/nonexistent")

    def write(self, name: str, content: bytes | str) -> Path:
        self._sandbox.files[name] = content
        return self.path / name

    def run(self, request: ExecRequest) -> ExecResult:
        self._sandbox.requests.append(request)
        if self._sandbox.on_run is not None:
            self._sandbox.on_run()
        if self._sandbox.results:
            return self._sandbox.results.pop(0)
        return ExecResult(exit_code=0, isolation=Isolation.CONTAINER)


class FakeSandbox:
    name = "fake"
    isolation = Isolation.CONTAINER
    limitations = frozenset()

    def __init__(self, results: list[ExecResult] | None = None) -> None:
        self.results = results or [
            ExecResult(exit_code=0, isolation=Isolation.CONTAINER),
            ExecResult(exit_code=0, stdout="hi\n", isolation=Isolation.CONTAINER),
        ]
        self.requests: list[ExecRequest] = []
        self.files: dict[str, bytes | str] = {}
        self.on_run = None

    @contextlib.contextmanager
    def workspace(self) -> Iterator[FakeWorkspace]:
        yield FakeWorkspace(self)


class World:
    def __init__(self, tmp_path: Path, sandbox: FakeSandbox | None = None) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.clock = Clock()
        self.sandbox = sandbox or FakeSandbox()
        self.images: list[str] = []

        def factory(image: str) -> FakeSandbox:
            self.images.append(image)
            return self.sandbox

        self.runner = CodeRunner(
            self.database,
            profiles_dir=PROFILES,
            worker="r1",
            sandbox_factory=factory,
            clock=self.clock,
        )
        version = sharif_judge.import_problem(
            EXAMPLE_TASK, course_id=COURSE, subject_profile=PROFILE, authored_by=AUTHOR
        )
        # 1 件目だけを公開サンプルにする（例題は全件が非公開）。
        cases = list(version.test_cases)
        cases[0] = cases[0].model_copy(update={"hidden": False})
        self.version = version.model_copy(update={"test_cases": tuple(cases)})
        with self.database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=self.version.task_id, course_id=COURSE, title="例題"))
            uow.tasks.save_version(self.version)
            uow.commit()
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")

    def set_course(self, overrides: dict) -> None:
        with self.database.unit_of_work() as uow:
            uow.identity.save_course(
                Course(
                    id=COURSE,
                    tenant_id=TENANT,
                    code="C1",
                    title="プログラミング",
                    term="2026-後期",
                    subject_profile=PROFILE,
                    grading_overrides=overrides,
                )
            )
            uow.commit()

    def ask(self, **kw) -> RunRequest:
        with self.database.unit_of_work() as uow:
            request = request_run(
                uow.run_requests,
                tenant_id=TENANT,
                learner_id=LEARNER,
                task_version_id=kw.pop("task_version_id", self.version.id),
                source=kw.pop("source", SOURCE),
                now=self.clock(),
                **kw,
            )
            uow.commit()
        return request

    def get(self, request: RunRequest) -> RunRequest:
        with self.database.unit_of_work() as uow:
            found = uow.run_requests.get(request.id)
        assert found is not None
        return found


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# -- 採点と同じ条件で動かす --------------------------------------------------


def test_a_c_program_is_compiled_then_run(world: World) -> None:
    request = world.ask(stdin="1 2\n")

    done = world.runner.run_once()

    assert done is not None and done.id == request.id
    assert done.state is RunState.DONE
    assert done.outcome is not None
    assert done.outcome.stage is RunStage.RUN
    assert done.outcome.stdout == "hi\n"
    assert done.outcome.isolation == "container"
    assert world.sandbox.files == {"main.c": SOURCE}

    compile_step, run_step = world.sandbox.requests
    # コンパイルのフラグは言語の表から来る（採点と同じ）。
    assert compile_step.argv == ("cc", "-std=c11", "-O0", "-o", "main", "main.c")
    assert compile_step.trusted_toolchain is True
    # **提出物そのものは信頼しない。**
    assert run_step.argv == ("./main",)
    assert run_step.trusted_toolchain is False
    assert run_step.network is False
    assert run_step.stdin == "1 2\n"
    assert world.get(request) == done


def test_the_limits_match_the_graders(world: World) -> None:
    """**IDE で動いたものが採点で動かない、を起こさない**（設計書 §4.2）。

    上限の形は評価器の `_limits` と同じでなければならない。私有の関数を
    読むのはここだけで、食い違いを見つけるためである。
    """
    for seconds in (0.5, 2.0, 5.0, 30.0):
        assert limits_for(seconds) == grading_limits(seconds)

    world.ask()
    world.runner.run_once()
    compile_step, run_step = world.sandbox.requests
    # 例題のプロファイルは case_timeout_seconds を 2 秒にしている。
    assert run_step.limits == grading_limits(2.0)
    assert compile_step.limits == grading_limits(30.0)


def test_the_courses_overrides_apply_as_they_do_in_grading(world: World) -> None:
    """コースの上書きは採点ワーカーと同じ重ね方で効く（ADR 0018）。"""
    world.set_course(
        {
            "evaluator_options": {
                "code_test_runner": {"case_timeout_seconds": 7, "image": "gcc:14-bookworm"}
            }
        }
    )
    world.ask()
    world.runner.run_once()

    assert world.images == ["gcc:14-bookworm"]
    assert world.sandbox.requests[-1].limits == grading_limits(7.0)


def test_a_compile_error_is_reported_as_such(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        [
            ExecResult(
                exit_code=1,
                stderr="main.c:1:1: error: expected ';'",
                isolation=Isolation.CONTAINER,
            )
        ]
    )
    world = World(tmp_path, sandbox)
    world.ask()

    done = world.runner.run_once()

    assert done is not None and done.state is RunState.DONE
    assert done.outcome is not None
    assert done.outcome.stage is RunStage.COMPILE
    assert "error" in done.outcome.stderr
    # コンパイルに失敗したら実行しない。
    assert len(sandbox.requests) == 1


def test_a_timeout_is_a_result_not_a_failure(tmp_path: Path) -> None:
    """**学習者のコードが落ちたのは FAILED ではない。** 無限ループも結果である。"""
    sandbox = FakeSandbox(
        [
            ExecResult(exit_code=0, isolation=Isolation.CONTAINER),
            ExecResult(exit_code=-9, timed_out=True, isolation=Isolation.CONTAINER),
        ]
    )
    world = World(tmp_path, sandbox)
    world.ask()

    done = world.runner.run_once()

    assert done is not None and done.state is RunState.DONE
    assert done.outcome is not None and done.outcome.timed_out


def test_long_output_is_clipped_for_the_screen(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        [
            ExecResult(exit_code=0, isolation=Isolation.CONTAINER),
            ExecResult(
                exit_code=0, stdout="x" * (DISPLAY_OUTPUT_CHARS + 10), isolation=Isolation.CONTAINER
            ),
        ]
    )
    world = World(tmp_path, sandbox)
    world.ask()

    done = world.runner.run_once()

    assert done is not None and done.outcome is not None
    assert len(done.outcome.stdout) == DISPLAY_OUTPUT_CHARS
    assert done.outcome.truncated


# -- サンプル ----------------------------------------------------------------


def test_a_public_sample_is_read_from_the_task(world: World) -> None:
    """サンプルの中身は**課題から**引く。学習者から届いたものは信じない。"""
    world.ask(sample_name="case1")
    world.runner.run_once()
    assert world.sandbox.requests[-1].stdin == "-1 0 1 2\n"


@pytest.mark.parametrize("name", ["case2", "no-such-case"])
def test_a_hidden_or_unknown_sample_is_refused_the_same_way(world: World, name: str) -> None:
    """**非公開のケースでは実行しない**（ADR 0024 §4）。しかも「無い」と同じ
    文言で断る ── 「非公開です」と言うと、その名前のケースがあることが漏れる。
    """
    request = world.ask(sample_name=name)

    done = world.runner.run_once()

    assert done is not None and done.id == request.id
    assert done.state is RunState.FAILED
    assert done.error == NO_SUCH_SAMPLE
    assert world.sandbox.requests == []


# -- 実行できないとき --------------------------------------------------------


def test_a_task_that_does_not_run_tests_cannot_be_run(world: World) -> None:
    """採点がテストを走らせない課題（レポート）には、言語も上限も無い。"""
    report = world.version.model_copy(
        update={
            "id": "tsv_" + "9" * 32,
            "task_id": "tsk_" + "9" * 32,
            "subject_profile": "report_ja",
        }
    )
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=report.task_id, course_id=COURSE, title="レポート"))
        uow.tasks.save_version(report)
        uow.commit()
    world.ask(task_version_id=report.id)

    done = world.runner.run_once()

    assert done is not None and done.state is RunState.FAILED
    assert done.error == NOT_RUNNABLE
    assert world.sandbox.requests == []


def test_a_broken_sandbox_fails_the_request_with_a_plain_message(tmp_path: Path) -> None:
    world = World(tmp_path)

    def broken(image: str):
        raise SandboxUnavailable("docker: permission denied on /var/run/docker.sock")

    world.runner._sandbox_factory = broken  # type: ignore[attr-defined]
    world.ask()

    done = world.runner.run_once()

    assert done is not None and done.state is RunState.FAILED
    # **内部の例外文を学習者に見せない**（パスや設定が漏れる）。
    assert done.error == SANDBOX_DOWN
    assert "docker" not in (done.error or "")


def test_warm_up_raises_when_no_sandbox_can_be_built(tmp_path: Path) -> None:
    """起動時に組み立てられなければ、起動に失敗させる（systemd に知らせる）。"""
    world = World(tmp_path)

    def broken(image: str):
        raise SandboxUnavailable("none")

    world.runner._sandbox_factory = broken  # type: ignore[attr-defined]
    with pytest.raises(SandboxUnavailable):
        world.runner.warm_up()


def test_the_sandbox_is_built_once_per_image(world: World) -> None:
    for _ in range(3):
        world.ask()
        world.runner.run_once()
        world.clock.advance(5)
    assert world.images == [""]


# -- 古い要求と遅れた結果 ----------------------------------------------------


def test_a_stale_request_is_not_run(world: World) -> None:
    """**30 秒以上待った要求は実行しない。** とうに書き換えたコードである。"""
    request = world.ask()
    world.clock.advance(31)

    assert world.runner.run_once() is None
    assert world.get(request).state is RunState.EXPIRED
    assert world.sandbox.requests == []


def test_a_result_arriving_after_the_lease_is_dropped(world: World) -> None:
    """リースが切れて FAILED になった要求に、遅れた結果を上書きしない。
    学習者の画面が「失敗」から「完了」に後から変わってしまう。
    """
    request = world.ask()

    def lease_runs_out() -> None:
        world.clock.advance(200)
        with world.database.unit_of_work() as uow:
            uow.run_requests.expire_stale(world.clock(), stale_after=30)
            uow.commit()

    world.sandbox.on_run = lease_runs_out
    world.runner.run_once()

    after = world.get(request)
    assert after.state is RunState.FAILED
    assert after.error == RUNNER_LOST


def test_finished_requests_are_purged_after_a_while(world: World) -> None:
    request = world.ask()
    world.runner.run_once()
    world.clock.advance(601)

    world.runner.run_once()

    with world.database.unit_of_work() as uow:
        assert uow.run_requests.get(request.id) is None


# -- 採点とは混ざらない ------------------------------------------------------


def test_the_runner_leaves_grading_jobs_alone(world: World) -> None:
    """**runner が止まっても採点は進み、採点が止まっても実行はできる**（I3）。

    提出で積まれた採点ジョブは、runner が何件処理しても待ったまま残り、
    採点ワーカーがいなくても実行は終わる。
    """
    service = SubmissionService(world.database.unit_of_work, world.store, clock=world.clock)
    service.accept(
        tenant_id=TENANT,
        task_version_id=world.version.id,
        learner_id=LEARNER,
        submitted_as=Role.LEARNER,
        subject_profile=PROFILE,
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=SOURCE.encode())],
    )
    world.ask()

    assert world.runner.run_once() is not None
    assert world.runner.run_once() is None

    with world.database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 1
        # 採点ワーカーの取得は実行要求を拾わない。
        job = uow.jobs.reserve(world.clock(), worker="w1", lease_seconds=60)
        assert job is not None
        assert uow.jobs.reserve(world.clock(), worker="w2", lease_seconds=60) is None
