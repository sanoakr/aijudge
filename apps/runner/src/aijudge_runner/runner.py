"""試しの実行を処理する（ADR 0024）。

1 件の流れ:

    古い要求を片付け、1 件取る（リースを取って commit）
      → 課題とコースを読み、採点と同じ手順で言語・イメージ・上限を決める
      → sandbox でコンパイルし、実行する
      → 結果を同じ行に書き戻して commit

**採点ワーカーと同じものを使い、同じ場所には書かない。** sandbox も言語の表も
実行上限も採点と同じだが、`grading_jobs` には触らず `GradingRun` も作らない。
runner が止まっても採点は進み、採点ワーカーが止まっても実行はできる。

**学習者のコードをログに出さない**（P7）。運用ログに載せるのは要求の ID と
状態と時間だけである。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aijudge_core import Course, TaskVersion
from aijudge_eval_code_test_runner import (
    DEFAULT_CASE_TIMEOUT_SECONDS,
    DEFAULT_COMPILE_TIMEOUT_SECONDS,
    EVALUATOR_ID,
    OPTION_CASE_TIMEOUT,
    OPTION_COMPILE_TIMEOUT,
)
from aijudge_grading import OverrideError, SubjectProfile, effective_profile, load_profile
from aijudge_ide import (
    DEFAULT_LEASE_SECONDS,
    EDITOR_FORMATS,
    STALE_AFTER_SECONDS,
    RunOutcome,
    RunRequest,
    RunStage,
    RunState,
    clip_for_display,
)
from aijudge_persistence import Database, SqlUnitOfWork
from aijudge_sandbox import ExecRequest, ExecResult, Limits, Sandbox, SandboxError, build_sandbox
from aijudge_telemetry import bind
from aijudge_toolchain import OPTION_IMAGE, Language, UnknownLanguage, resolve_language

logger = logging.getLogger(__name__)

# 終わった要求をどれだけ残すか（秒）。画面は結果が出るまで 0.5 秒ごとに
# 問い合わせ、出たら止まる。**結果は残さない**（ADR 0024 §1）ので、取りに
# 来るのが遅れた画面の分だけ残せば足りる。
RETAIN_FINISHED_SECONDS = 600.0
# 片付けを走らせる間隔（秒）。要求ごとに走らせると、空振りの DELETE を
# 250 ms ごとに打つことになる。
PURGE_INTERVAL_SECONDS = 60.0

# 学習者に見せる文言。**内部の例外文を入れない**（パスや設定値が漏れる）。
NOT_RUNNABLE = "この課題はエディタでの実行に対応していません。"
NO_TASK = "課題が見つかりません。"
NO_SUCH_SAMPLE = "そのサンプルはありません。"
MISCONFIGURED = "この課題の実行設定に誤りがあります。担当の教員にお知らせください。"
SANDBOX_DOWN = "実行環境が利用できません。しばらくしてからもう一度実行してください。"


class RunNotPossible(Exception):
    """この要求は実行できない。`message` は学習者に見せる文言。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class RunPlan:
    """1 件をどう動かすか。採点と同じ手順で決めたもの。"""

    language: Language
    image: str
    compile_limits: Limits
    run_limits: Limits
    stdin: str


def limits_for(timeout_seconds: float) -> Limits:
    """1 回の実行の上限。**採点（`code_test_runner`）と同じ形。**

    CPU 秒は壁時計の切り上げ、メモリ・プロセス数・出力は sandbox の既定
    （512 MiB・64・1 MiB）で、評価器の定数と同じ値である。食い違えば、IDE で
    動いたものが採点で動かない（設計書 §4.2）── 一致はテストで固定してある。
    """
    return Limits(cpu_seconds=max(1, math.ceil(timeout_seconds)), wall_seconds=timeout_seconds)


def seconds_option(options: dict[str, object], key: str, default: float) -> float:
    """プロファイルの秒数を読む。不正な値は既定に落とす（評価器と同じ扱い）。"""
    raw = options.get(key, default)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _default_sandbox_factory(image: str) -> Sandbox:
    return build_sandbox(image=image) if image else build_sandbox()


class CodeRunner:
    def __init__(
        self,
        database: Database,
        *,
        profiles_dir: Path,
        worker: str = "runner-1",
        sandbox_factory: Callable[[str], Sandbox] = _default_sandbox_factory,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        stale_after_seconds: float = STALE_AFTER_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._profiles_dir = profiles_dir
        self._worker = worker
        self._sandbox_factory = sandbox_factory
        self._lease_seconds = lease_seconds
        self._stale_after = stale_after_seconds
        self._clock = clock
        self._profiles: dict[str, SubjectProfile] = {}
        # イメージごとに 1 つ。**要求ごとに作らない** ── `DockerSandbox` の
        # 初期化は書き込み確認でコンテナを立てるので、毎回やると 1 件あたり
        # 数百 ms 余計にかかる（ADR 0024 §2）。
        self._sandboxes: dict[str, Sandbox] = {}
        self._last_purge: datetime | None = None

    def warm_up(self) -> Sandbox:
        """既定の sandbox を組み立てる。**起動時に 1 度だけ呼ぶ。**

        組み立てられなければ例外のまま上げる ── 実行できない runner が要求を
        取っては失敗させ続けるより、起動に失敗して systemd に知らせる方がよい。
        """
        return self._sandbox_for("")

    # -- 1 件処理 ----------------------------------------------------------

    def run_once(self) -> RunRequest | None:
        """1 件処理する。待っている要求が無ければ None。"""
        now = self._clock()
        with self._database.unit_of_work() as uow:
            uow.run_requests.expire_stale(now, stale_after=self._stale_after)
            self._purge_if_due(uow, now)
            request = uow.run_requests.reserve(
                now, worker=self._worker, lease_seconds=self._lease_seconds
            )
            # 取ったことを先に commit する。実行のあいだ他の runner から
            # 見えていなければ、同じ要求を二度動かす（採点ワーカーと同じ理由）。
            uow.commit()
        if request is None:
            return None

        with bind(run_request_id=str(request.id), task_version_id=str(request.task_version_id)):
            try:
                plan = self._plan(request)
                outcome = self._execute(request, plan)
            except RunNotPossible as exc:
                logger.info("run request refused: %s", type(exc).__name__)
                return self._finish(request, error=exc.message)
            except SandboxError:
                logger.exception("the sandbox failed")
                return self._finish(request, error=SANDBOX_DOWN)
            logger.info(
                "ran a request",
                extra={
                    "stage": outcome.stage.value,
                    "exit_code": outcome.exit_code,
                    "timed_out": outcome.timed_out,
                    "duration_ms": outcome.duration_ms,
                },
            )
            return self._finish(request, outcome=outcome)

    def run_until_empty(self) -> int:
        """待っている要求が無くなるまで処理する。処理した件数を返す。"""
        count = 0
        while self.run_once() is not None:
            count += 1
        return count

    # -- internals ---------------------------------------------------------

    def _purge_if_due(self, uow: SqlUnitOfWork, now: datetime) -> None:
        if self._last_purge is not None and now - self._last_purge < timedelta(
            seconds=PURGE_INTERVAL_SECONDS
        ):
            return
        uow.run_requests.purge_finished(now - timedelta(seconds=RETAIN_FINISHED_SECONDS))
        self._last_purge = now

    def _plan(self, request: RunRequest) -> RunPlan:
        with self._database.unit_of_work() as uow:
            version = uow.tasks.get_version(request.task_version_id)
            if version is None:
                raise RunNotPossible(NO_TASK)
            task = uow.tasks.get_task(version.task_id)
            course = None if task is None else uow.identity.get_course(task.course_id)

        profile = self._profile(version.subject_profile, course)
        # **採点がテストを走らせる課題だけ**実行させる。走らせない課題
        # （レポート・画像）には言語も上限も決まっていない。
        if EVALUATOR_ID not in profile.deterministic:
            raise RunNotPossible(NOT_RUNNABLE)
        options = dict(profile.evaluator_options.get(EVALUATOR_ID, {}))
        try:
            language = resolve_language(options)
        except UnknownLanguage as exc:
            raise RunNotPossible(MISCONFIGURED) from exc
        # **選んだ形式が採点の言語と一致するときだけ動かす**（設計書 §4.2）。
        # web も同じ検査をしているが、ここでもう一度確かめる ── 採点が C の
        # 課題で Python を走らせても、採点で何が起きるかの手がかりにならない。
        if request.suffix is not None:
            chosen = EDITOR_FORMATS.get(request.suffix)
            if chosen is None or chosen.filename != language.source_name:
                raise RunNotPossible(NOT_RUNNABLE)

        return RunPlan(
            language=language,
            image=str(options.get(OPTION_IMAGE, "")).strip(),
            compile_limits=limits_for(
                seconds_option(options, OPTION_COMPILE_TIMEOUT, DEFAULT_COMPILE_TIMEOUT_SECONDS)
            ),
            run_limits=limits_for(
                seconds_option(options, OPTION_CASE_TIMEOUT, DEFAULT_CASE_TIMEOUT_SECONDS)
            ),
            stdin=self._stdin(request, version),
        )

    def _stdin(self, request: RunRequest, version: TaskVersion) -> str:
        """標準入力。サンプルなら**課題から**引く。

        **非公開のケースでは実行しない**（ADR 0024 §4）。試験中のテスト結果は
        答えの一部である。非公開の名前を指定されても「無い」と答える ──
        「非公開です」と答えると、その名前のケースがあることが漏れる。
        """
        if request.sample_name is None:
            return request.stdin
        for case in version.test_cases:
            if case.evaluator_id == EVALUATOR_ID and case.name == request.sample_name:
                if case.hidden:
                    break
                return str(case.payload.get("input", ""))
        raise RunNotPossible(NO_SUCH_SAMPLE)

    def _profile(self, name: str, course: Course | None) -> SubjectProfile:
        """このコースに効く採点設定。**採点ワーカーと同じ重ね方**（ADR 0018）。

        評価器の登録簿は渡さない。ここが読むのは `code_test_runner` の
        オプションだけで、評価器の実在の検査は採点ワーカーの仕事である
        （渡すと、AI 評価器まで runner に読み込むことになる）。
        """
        if name not in self._profiles:
            path = self._profiles_dir / f"{name}.yaml"
            if not path.is_file():
                raise RunNotPossible(MISCONFIGURED)
            try:
                self._profiles[name] = load_profile(path)
            except Exception as exc:
                raise RunNotPossible(MISCONFIGURED) from exc
        base = self._profiles[name]
        if course is None or not course.grading_overrides:
            return base
        try:
            return effective_profile(base, course.grading_overrides)
        except OverrideError as exc:
            raise RunNotPossible(MISCONFIGURED) from exc

    def _sandbox_for(self, image: str) -> Sandbox:
        if image not in self._sandboxes:
            self._sandboxes[image] = self._sandbox_factory(image)
        return self._sandboxes[image]

    def _execute(self, request: RunRequest, plan: RunPlan) -> RunOutcome:
        sandbox = self._sandbox_for(plan.image)
        language = plan.language
        with sandbox.workspace() as workspace:
            workspace.write(language.source_name, request.source)
            if language.compile_argv is not None:
                compiled = workspace.run(
                    ExecRequest(
                        argv=language.compile_argv,
                        limits=plan.compile_limits,
                        # コンパイラは信頼できる実行体。提出物そのものを
                        # 動かすときは決して立てない（採点と同じ）。
                        trusted_toolchain=True,
                    )
                )
                if not compiled.ok:
                    return _outcome(RunStage.COMPILE, compiled)
            result = workspace.run(
                ExecRequest(argv=language.run_argv, stdin=plan.stdin, limits=plan.run_limits)
            )
        return _outcome(RunStage.RUN, result)

    def _finish(
        self,
        request: RunRequest,
        *,
        outcome: RunOutcome | None = None,
        error: str | None = None,
    ) -> RunRequest:
        """結果を書き戻す。**まだ自分が握っているときだけ。**

        実行が長引いてリースが切れると、受付や他の runner が要求を FAILED に
        している。そこへ上書きすると、学習者の画面が「失敗」から「完了」に
        後から変わる。遅れた結果は捨てる。
        """
        now = self._clock()
        with self._database.unit_of_work() as uow:
            current = uow.run_requests.get(request.id)
            if (
                current is None
                or current.state is not RunState.RUNNING
                or current.worker != self._worker
            ):
                logger.warning("dropped a result whose lease had been lost")
                return current or request
            finished = (
                current.completed(now, outcome)
                if outcome is not None
                else current.failed(now, error or SANDBOX_DOWN)
            )
            uow.run_requests.update(finished)
            uow.commit()
        return finished


def _outcome(stage: RunStage, result: ExecResult) -> RunOutcome:
    stdout, cut_out = clip_for_display(result.stdout)
    stderr, cut_err = clip_for_display(result.stderr)
    return RunOutcome(
        stage=stage,
        exit_code=result.exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        signal_name=result.signal_name,
        truncated=result.truncated or cut_out or cut_err,
        isolation=result.isolation.value,
    )
