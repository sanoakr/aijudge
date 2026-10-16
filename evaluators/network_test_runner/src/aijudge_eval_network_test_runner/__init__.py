"""クライアント／サーバ課題を伴走プロセスで採点する決定的評価器。

Sharif Judge が採点できなかった課題がここに入る（ADR 0008）。

- **クライアント課題** — 提出が接続する。伴走**サーバ**を立てる
- **サーバ課題** — 提出が待ち受ける。伴走**クライアント**を走らせる

伴走プロセスは**教材から取り込む**。生成しない。`echoServer.py` は課題文が
名指ししている相手で、それに対して採点することが「課題の指示どおりか」の
定義そのものになる。LLM に別実装を書かせると課題文と採点基準がずれるし、
決定的評価に AI の判断が混ざる（設計原則 P3、ADR 0008）。

**サンドボックスの契約を変えていない。** 伴走プロセスと提出物を同一コンテナ内の
2 プロセスにし、`--network=none` のまま loopback で通信させる。外部への到達は
塞がったままで、隔離を課題の都合で緩めない。段取りは作業域に書き出す起動
スクリプトが持つ（`launcher.py`）。

期待値の照合は**部分一致**（`expected_contains`）を既定にしてある。サーバの
出力には接続元の一時ポート（`('127.0.0.1', 53578)`）のように毎回変わる値が
混ざるため、完全一致では常に落ちる。
"""

from __future__ import annotations

import math
import signal
from typing import Any

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest
from aijudge_sandbox import (
    ExecRequest,
    Limits,
    Sandbox,
    SandboxError,
    Workspace,
    default_sandbox,
)
from aijudge_toolchain import OPTION_IMAGE, Language, UnknownLanguage, resolve_language

from . import launcher

EVALUATOR_ID = "network_test_runner"
# 課題の入力の `{host}` を埋める宛先。定義は 1 か所（`launcher.COMPANION_HOST`）。
COMPANION_HOST = launcher.COMPANION_HOST

ROLE_CLIENT = "client"
ROLE_SERVER = "server"

# 待ち受け開始を待つ上限。提出物の起動が遅いだけで落とさない程度。
DEFAULT_READY_TIMEOUT_SECONDS = 5.0
OPTION_READY_TIMEOUT = "ready_timeout_seconds"

# 前景プロセス 1 回の上限。
DEFAULT_CASE_TIMEOUT_SECONDS = 10.0
OPTION_CASE_TIMEOUT = "case_timeout_seconds"

# 伴走プロセスを含めた全体。起動スクリプト自体の上限。
_OVERHEAD_SECONDS = 15.0

_MEMORY_BYTES = 512 * 1024 * 1024
# 伴走プロセス＋提出物＋起動スクリプトで最低 3 つ。スレッドを使う課題があるので余裕を取る。
_MAX_PROCESSES = 64
_MAX_OUTPUT_BYTES = 1024 * 1024

_EXCERPT_CHARS = 2000
# 根拠（学習者に返る説明）に載せるエラーの長さ。全文は raw_output に残る。
_ERROR_EXCERPT_CHARS = 300

# 起動スクリプトが待ち受け側を止めるときの終了コード（`background.kill()`）。
# **これは提出物の異常終了ではない。** サーバ役の提出物は止められるまで動き続けるのが
# 正しい姿なので、この値を「落ちた」と読むと、正しいサーバが全部異常終了に見える。
_KILLED_BY_LAUNCHER = -signal.SIGKILL

# 理由の語は code_test_runner と揃える。コンソールの表示（`io_results._reason`）が
# 両方を同じ語で読む。
REASON_TIMEOUT = "timeout"
REASON_NONZERO_EXIT = "nonzero exit"
REASON_OUTPUT_MISMATCH = "output mismatch"


def _last_error_line(case: dict[str, object]) -> str:
    """エラー出力の要点。Python の traceback は最後の行に型と説明が出る。"""
    lines = [line.strip() for line in str(case.get("stderr", "")).splitlines() if line.strip()]
    return lines[-1][:_ERROR_EXCERPT_CHARS] if lines else ""


def _common_error(failed: list[dict[str, object]]) -> str | None:
    """落ちたケースに共通する、出力の照合より前の理由。無ければ None（#626）。

    **「出力に現れなかったもの」より先に言う。** 起動直後に落ちた提出に期待する
    出力を並べても、学習者には「何かが足りない」としか読めない ── 実際に
    2026-10-16 の test5 では、構文エラーや `with int(input())` の TypeError で
    1 行も出力しなかった提出に、その説明が返っていた。
    """
    if all(case.get("reason") == REASON_TIMEOUT for case in failed):
        return (
            "プログラムが時間内に終わりませんでした"
            "（入力を待ち続けている、相手の応答を待ち続けている、など）。"
        )
    crashed = [case for case in failed if case.get("reason") == REASON_NONZERO_EXIT]
    if not crashed:
        return None
    message = _last_error_line(crashed[0])
    if not message:
        return f"プログラムがエラーで止まりました（終了コード {crashed[0].get('exit_code')}）。"
    if len(crashed) == len(failed):
        return f"プログラムがエラーで止まりました: {message}"
    return f"プログラムがエラーで止まったケースがあります: {message}"


def _unbuffered(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Python なら出力をため込ませない（`-u`）。

    **時間切れで止められると、ため込んだ出力は消える**（#626）。パイプへの出力は
    Python がブロック単位でため込むので、途中まで正しく動いていても「出力なし」として
    記録され、教員も学習者も何が起きたか読めなかった。`-I` は環境変数
    （`PYTHONUNBUFFERED`）を読まないので、引数で渡す。

    言語の定義（`aijudge_toolchain`）は変えない ── code_test_runner と共有しており、
    出力の多い提出を gVisor の上でため込まずに書くと、1 行ごとのシステムコールで遅くなる。
    """
    if argv and argv[0].startswith("python") and "-u" not in argv:
        return (argv[0], "-u", *argv[1:])
    return argv


class PayloadError(Exception):
    """テストケースの宣言が壊れている。

    採点を失敗として返す。0 点にしない（宣言の誤りは学習者の責任ではない）。
    """


def _seconds(options: dict[str, object], key: str, default: float) -> float:
    raw = options.get(key, default)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _text(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def _strings(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key, ())
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


class NetworkTestRunner:
    """伴走プロセスを立てて採点する。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC
    # 入出力セット（`EvaluationRequest.test_cases`）を読む（#300）。
    # **画面はこの宣言で欄を出す** ── 評価器名の表を画面に持たせると、
    # 評価器を足した日にその表だけが古くなる。
    uses_test_cases = True
    # **入出力の組ではない**（#402）。1 件が伴走プロセス・ポート・同梱ファイル・
    # 期待する断片（`expected_contains`）を持つ。名乗らないと画面は入出力の欄で
    # 編集させ、保存で `input` / `expected` 以外の中身が消えて評価器も
    # `code_test_runner` に書き換わる。画面が知らない形なので、編集欄は出ず
    # 課題定義から直す。
    test_case_shape = "companion"

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        self._sandbox = sandbox
        self._explicit = sandbox is not None
        self._by_image: dict[str, Sandbox] = {}

    def _resolve_sandbox(self, options: dict[str, object]) -> Sandbox:
        if self._explicit and self._sandbox is not None:
            return self._sandbox
        image = str(options.get(OPTION_IMAGE, "")).strip()
        if not image:
            if self._sandbox is None:
                self._sandbox = default_sandbox()
            return self._sandbox
        if image not in self._by_image:
            from aijudge_sandbox import build_sandbox

            self._by_image[image] = build_sandbox(image=image)
        return self._by_image[image]

    # -- 採点 --------------------------------------------------------------

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = self._target_criterion(request)
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion is assigned to this evaluator"},
            )
        cases = tuple(case for case in request.test_cases if case.evaluator_id == EVALUATOR_ID)
        if not cases:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED, raw_output={"reason": "no test cases"}
            )

        source_id, source = self._source(request)
        if source is None or source_id is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error="no source artifact found in the submission",
            )

        try:
            language = resolve_language(request.options)
        except UnknownLanguage as exc:
            return EvaluationOutcome(status=EvaluatorStatus.FAILED, error=str(exc))

        ready_timeout = _seconds(
            request.options, OPTION_READY_TIMEOUT, DEFAULT_READY_TIMEOUT_SECONDS
        )
        run_timeout = min(
            request.timeout_seconds,
            _seconds(request.options, OPTION_CASE_TIMEOUT, DEFAULT_CASE_TIMEOUT_SECONDS),
        )

        results: list[dict[str, object]] = []
        try:
            sandbox = self._resolve_sandbox(request.options)
            for case in cases:
                payload = dict(case.payload)
                with sandbox.workspace() as workspace:
                    results.append(
                        self._run_case(
                            workspace,
                            language,
                            source,
                            case.name,
                            float(case.weight),
                            payload,
                            ready_timeout=ready_timeout,
                            run_timeout=run_timeout,
                        )
                    )
        except PayloadError as exc:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED, error=f"malformed test case: {exc}"
            )
        except SandboxError as exc:
            # 隔離できないなら採点しない（ADR 0006）。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"cannot execute the submission safely: {exc}",
            )

        passed_weight = sum(float(r["weight"]) for r in results if r["passed"])
        total_weight = sum(float(r["weight"]) for r in results) or 1.0
        ratio = passed_weight / total_weight
        failed = [r for r in results if not r["passed"]]

        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=(
                self._score(
                    criterion_id=criterion.id,
                    weight=criterion.weight,
                    ratio=ratio,
                    level=self._level_for(criterion, ratio),
                    artifact_id=source_id,
                    content_hash=self._content_hash(request, source_id),
                    rationale=self._rationale(len(results) - len(failed), len(results), failed),
                ),
            ),
            raw_output={
                "cases": results,
                "sandbox": sandbox.name,
                "isolation": sandbox.isolation.value,
            },
        )

    # -- 1 ケース ----------------------------------------------------------

    def _run_case(
        self,
        workspace: Workspace,
        language: Language,
        source: bytes,
        name: str,
        weight: float,
        payload: dict[str, Any],
        *,
        ready_timeout: float,
        run_timeout: float,
    ) -> dict[str, object]:
        role = _text(payload, "role")
        if role not in (ROLE_CLIENT, ROLE_SERVER):
            raise PayloadError(f"role must be {ROLE_CLIENT!r} or {ROLE_SERVER!r}, got {role!r}")

        companion = _text(payload, "companion")
        if not companion:
            raise PayloadError("companion source is missing")
        companion_name = _text(payload, "companion_name", "companion.py")
        port = payload.get("port")
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise PayloadError(f"port must be an int in [1024, 65535], got {port!r}")

        workspace.write(language.source_name, source)
        workspace.write(companion_name, companion)
        for filename, content in (payload.get("fixtures") or {}).items():
            workspace.write(str(filename), str(content))

        submission_argv = _unbuffered(language.run_argv)
        companion_argv = ("python3", "-u", "-I", companion_name)
        # `{host}` と `{port}` を埋める。課題の入力には本番のホスト・ポートが
        # 書かれているので、伴走プロセスの値に差し替える必要がある。
        fill = {"host": launcher.COMPANION_HOST, "port": str(port)}
        submission_stdin = _text(payload, "input").format(**fill)
        companion_stdin = _text(payload, "companion_input").format(**fill)

        if role == ROLE_CLIENT:
            # 伴走サーバを先に立て、待ち受けを確認してから提出を走らせる。
            script = launcher.render(
                port=port,
                background_argv=companion_argv,
                background_stdin=companion_stdin,
                foreground_argv=submission_argv,
                foreground_stdin=submission_stdin,
                background_role="companion",
                ready_timeout=ready_timeout,
                run_timeout=run_timeout,
            )
        else:
            # 提出を先に立て、待ち受けを確認してから伴走クライアントを走らせる。
            script = launcher.render(
                port=port,
                background_argv=submission_argv,
                background_stdin=submission_stdin,
                foreground_argv=companion_argv,
                foreground_stdin=companion_stdin,
                background_role="submission",
                ready_timeout=ready_timeout,
                run_timeout=run_timeout,
            )
        workspace.write(launcher.LAUNCHER_NAME, script)

        result = workspace.run(
            ExecRequest(
                argv=("python3", "-I", launcher.LAUNCHER_NAME),
                limits=Limits(
                    cpu_seconds=max(1, math.ceil(run_timeout + ready_timeout + _OVERHEAD_SECONDS)),
                    wall_seconds=run_timeout + ready_timeout + _OVERHEAD_SECONDS,
                    memory_bytes=_MEMORY_BYTES,
                    processes=_MAX_PROCESSES,
                    output_bytes=_MAX_OUTPUT_BYTES,
                ),
            )
        )

        entry: dict[str, object] = {"name": name, "weight": weight, "role": role, "port": port}
        if result.timed_out:
            return entry | {"passed": False, "reason": "timeout"}

        try:
            report = launcher.parse(result.stdout)
        except ValueError as exc:
            # 段取りが失敗した。**0 点にしない。** 提出物の問題ではない。
            raise PayloadError(f"{name}: {exc}") from exc

        if not report.get("ready"):
            waited_on = "提出したサーバ" if role == ROLE_SERVER else "伴走サーバ"
            return entry | {
                "passed": False,
                "reason": "not_listening",
                "detail": (
                    f"{waited_on}が {ready_timeout:.0f} 秒以内に"
                    f"ポート {port} で待ち受けを始めませんでした"
                ),
                "background_stderr": str(report.get("background", {}).get("stderr", ""))[
                    :_EXCERPT_CHARS
                ],
            }

        submission_out, companion_out = self._split(role, report)
        entry |= {
            "submission_stdout": submission_out[:_EXCERPT_CHARS],
            "companion_stdout": companion_out[:_EXCERPT_CHARS],
        }
        # **提出物がどう終わったかを残す**（#626）。以前は標準出力しか残さず、
        # 起動直後の構文エラーも、終わらずに打ち切られたプログラムも「出力が一致しない・
        # 出力なし」と同じ顔で記録された。2026-10-16 の network test5 では 96 件中
        # 約 65 件がそうなり、原因を知るには運用機で評価器を動かし直すしかなかった。
        submission = self._submission_report(role, report)
        stderr = str(submission.get("stderr") or "").strip()
        if stderr:
            entry["stderr"] = stderr[-_EXCERPT_CHARS:]
        exit_code = submission.get("returncode")
        if isinstance(exit_code, int):
            entry["exit_code"] = exit_code
        foreground_timed_out = bool(report.get("foreground", {}).get("timed_out"))
        if foreground_timed_out:
            entry["timed_out"] = True

        missing = [
            needle
            for needle in _strings(payload, "expected_contains")
            if needle not in submission_out
        ]
        missing_companion = [
            needle
            for needle in _strings(payload, "companion_expected_contains")
            if needle not in companion_out
        ]
        if missing or missing_companion:
            # **判定は変えない。** 通るかどうかは今までどおり出力だけで決まる。
            # 変えるのは、落ちたときに残す理由だけである。
            return entry | {
                "passed": False,
                "reason": self._failure_reason(role, foreground_timed_out, exit_code),
                "missing": missing,
                "missing_companion": missing_companion,
            }
        return entry | {"passed": True}

    @staticmethod
    def _submission_report(role: str, report: dict) -> dict:
        """起動スクリプトの報告のうち、提出物のプロセスの分。"""
        key = "foreground" if role == ROLE_CLIENT else "background"
        value = report.get(key, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _failure_reason(role: str, foreground_timed_out: bool, exit_code: object) -> str:
        """落ちたケースの理由。**時間切れ → 異常終了 → 出力の不一致**の順に見る。

        サーバ役の提出物では、前景は伴走クライアントである。その時間切れは
        「提出物のサーバが応答を返さなかった」ことを意味するので、同じく時間切れとする。
        サーバ役が起動スクリプトに止められた終了コード（`_KILLED_BY_LAUNCHER`）は
        異常終了ではない。
        """
        if foreground_timed_out:
            return REASON_TIMEOUT
        crashed = isinstance(exit_code, int) and exit_code not in (0, _KILLED_BY_LAUNCHER)
        if crashed:
            return REASON_NONZERO_EXIT
        return REASON_OUTPUT_MISMATCH

    @staticmethod
    def _split(role: str, report: dict) -> tuple[str, str]:
        """提出物の出力と伴走プロセスの出力を分ける。

        どちらが前景かは役割で決まる。取り違えると、伴走プロセスの出力を
        提出物の出力として照合してしまい、何を出しても通る。
        """
        foreground = str(report.get("foreground", {}).get("stdout", ""))
        background = str(report.get("background", {}).get("stdout", ""))
        if role == ROLE_CLIENT:
            return foreground, background
        return background, foreground

    # -- 出力の整形 --------------------------------------------------------

    def _rationale(self, passed: int, total: int, failed: list[dict[str, object]]) -> str:
        if not failed:
            return f"伴走プロセスとの通信 {total} 件すべてが期待どおりでした。"
        head = f"{total} 件中 {passed} 件が期待どおりでした。"

        not_listening = [case for case in failed if case.get("reason") == "not_listening"]
        if len(not_listening) == len(failed):
            return head + str(not_listening[0].get("detail", "待ち受けが始まりませんでした。"))

        common = _common_error(failed)
        if common:
            return head + common

        missing: list[str] = []
        for case in failed:
            for needle in list(case.get("missing", ()))[:2]:  # type: ignore[arg-type]
                missing.append(str(needle))
        if missing:
            shown = " / ".join(dict.fromkeys(missing))[:200]
            return head + f"出力に現れなかったもの: {shown}"
        names = ", ".join(str(case["name"]) for case in failed[:5])
        return head + f"不一致: {names}。"

    def _level_for(self, criterion, ratio: float) -> int:
        best = criterion.levels[0]
        for level in criterion.levels:
            if ratio + 1e-9 >= level.score_ratio:
                best = level
        return best.level

    def _score(
        self,
        *,
        criterion_id,
        weight: float,
        ratio: float,
        level: int,
        artifact_id: ArtifactId,
        content_hash: str,
        rationale: str,
    ) -> CriterionScore:
        return CriterionScore(
            id=CriterionScoreId(new_id("cs")),
            criterion_id=criterion_id,
            evaluator_result_id=EvaluatorResultId(new_id("evr")),
            kind=EvaluatorKind.DETERMINISTIC,
            level=level,
            score_ratio=ratio,
            weight=weight,
            confidence=1.0,
            # 決定的に確定する。AI の判断で覆されない（設計原則 P3）。
            conclusive=True,
            evidence=(
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=WholeSpan(),
                    note=rationale,
                ),
            ),
            rationale=rationale,
        )

    # -- 入力の取り出し ----------------------------------------------------

    @staticmethod
    def _target_criterion(request: EvaluationRequest):
        if request.criterion is not None:
            return request.criterion
        for criterion in request.task_version.criteria:
            if criterion.evaluator_id == EVALUATOR_ID:
                return criterion
        return None

    @staticmethod
    def _source(request: EvaluationRequest) -> tuple[ArtifactId | None, bytes | None]:
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if content is not None:
                return artifact.id, content
        return None, None

    @staticmethod
    def _content_hash(request: EvaluationRequest, artifact_id: ArtifactId) -> str:
        for artifact in request.submission.artifacts:
            if artifact.id == artifact_id:
                return artifact.content_hash
        return "sha256:unknown"


def build() -> NetworkTestRunner:
    return NetworkTestRunner()
