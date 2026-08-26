"""C プログラムをテストケースで採点する決定的評価器。

Sharif Judge の中核機能に相当する。標準入力を与えて標準出力を比較する。

.. warning::

   **このモジュールは提出コードを直接 subprocess で実行する。**
   隔離は行っていない。悪意ある提出（ファイル削除、ネットワーク送信、
   fork bomb）を防げないため、**実学生の提出には絶対に使わないこと。**
   PoC-0 でサンドボックス（S4 / gVisor）に載せ替えるまでは、
   自分で書いた提出を通す検証用に限る。実行時間とプロセス数の上限だけは
   最低限かけてあるが、これは事故防止であって防御ではない。

判定は決定的（`conclusive=True`）。ここで不正解が確定した観点は
AI 評価器に問い合わせず、AI の判断で覆されることもない（設計原則 P3）。
"""

from __future__ import annotations

import math
import resource
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

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

EVALUATOR_ID = "code_test_runner"

# 事故防止の上限。防御ではない（モジュール docstring 参照）。
_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
_MAX_PROCESSES = 64
_MAX_OUTPUT_BYTES = 1 * 1024 * 1024


def _apply_limit(which: int, soft: int) -> None:
    """1 つの rlimit を、ハード上限を超えない範囲で設定する。

    macOS では RLIMIT_AS / RLIMIT_NPROC の設定が環境によって失敗する。
    preexec_fn の中で例外を投げると subprocess ごと起動できなくなるため、
    設定できないものは黙って諦める。どのみちこれは防御ではなく事故防止で、
    本来の隔離はサンドボックス（S4）の仕事。
    """
    try:
        _, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            soft = min(soft, hard)
        resource.setrlimit(which, (soft, hard))
    except (ValueError, OSError):
        pass


def _make_limiter(timeout_seconds: float) -> Callable[[], None]:
    """CPU 上限は待ち時間の設定に追従させる。

    壁時計のタイムアウトだけだと、CPU を回し続ける提出が上限いっぱい
    走ってから殺される。CPU 上限を先に当てておくと早く諦められる。
    """
    cpu_seconds = max(1, math.ceil(timeout_seconds))

    def limit() -> None:
        _apply_limit(resource.RLIMIT_CPU, cpu_seconds)
        _apply_limit(resource.RLIMIT_AS, _ADDRESS_SPACE_BYTES)
        _apply_limit(resource.RLIMIT_NPROC, _MAX_PROCESSES)
        _apply_limit(resource.RLIMIT_FSIZE, _MAX_OUTPUT_BYTES)

    return limit


def normalize_output(text: str) -> list[str]:
    """出力比較用の正規化。

    行末の空白と末尾の空行だけを無視する。Sharif Judge の既定と揃えてある。
    それ以上緩めない（「2  2  2.000」を正解にしない）のは、
    書式指定も課題の一部だから。
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


class CodeTestRunner:
    """コンパイルして実行し、期待出力と比較する。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def __init__(self, compiler: str | None = None) -> None:
        self._compiler = compiler or shutil.which("cc") or shutil.which("gcc") or "cc"

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = self._target_criterion(request)
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion is assigned to this evaluator"},
            )
        if not request.test_cases:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no test cases"},
            )

        source_id, source = self._source(request)
        if source is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error="no source artifact found in the submission",
            )

        with tempfile.TemporaryDirectory(prefix="aijudge-run-") as workdir:
            root = Path(workdir)
            source_path = root / "main.c"
            source_path.write_bytes(source)
            binary_path = root / "main"

            compiled = subprocess.run(
                [self._compiler, "-std=c11", "-O0", "-o", str(binary_path), str(source_path)],
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                cwd=root,
            )
            if compiled.returncode != 0:
                # コンパイルエラーも「0 点で確定」であって評価器の失敗ではない。
                return EvaluationOutcome(
                    status=EvaluatorStatus.OK,
                    scores=(
                        self._score(
                            criterion_id=criterion.id,
                            weight=criterion.weight,
                            ratio=0.0,
                            level=criterion.levels[0].level,
                            artifact_id=source_id,
                            content_hash=self._content_hash(request, source_id),
                            rationale="コンパイルに失敗したため、テストを実行できませんでした。",
                            note=compiled.stderr.strip()[:2000],
                        ),
                    ),
                    raw_output={"compile_error": compiled.stderr.strip()[:8000]},
                )

            cases = self._run_cases(request, binary_path, root)

        passed_weight = sum(case["weight"] for case in cases if case["passed"])
        total_weight = sum(case["weight"] for case in cases) or 1.0
        ratio = passed_weight / total_weight
        level = self._level_for(criterion, ratio)
        failed = [case for case in cases if not case["passed"]]

        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=(
                self._score(
                    criterion_id=criterion.id,
                    weight=criterion.weight,
                    ratio=ratio,
                    level=level,
                    artifact_id=source_id,
                    content_hash=self._content_hash(request, source_id),
                    rationale=self._rationale(len(cases) - len(failed), len(cases), failed),
                    note=None,
                ),
            ),
            raw_output={"cases": cases},
        )

    # -- internals ---------------------------------------------------------

    def _target_criterion(self, request: EvaluationRequest):
        """この評価器が担当する観点を選ぶ。

        観点側の `evaluator_id` 指定を優先し、無ければ最初の観点を採る。
        """
        explicit = [c for c in request.task_version.criteria if c.evaluator_id == EVALUATOR_ID]
        if explicit:
            return explicit[0]
        unassigned = [c for c in request.task_version.criteria if c.evaluator_id is None]
        return unassigned[0] if unassigned else None

    def _source(self, request: EvaluationRequest) -> tuple[ArtifactId | None, bytes | None]:
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if content is not None and artifact.kind.value == "code":
                return artifact.id, content
        return None, None

    def _content_hash(self, request: EvaluationRequest, artifact_id: ArtifactId) -> str:
        for artifact in request.submission.artifacts:
            if artifact.id == artifact_id:
                return artifact.content_hash
        return "unknown"

    def _run_cases(
        self, request: EvaluationRequest, binary: Path, cwd: Path
    ) -> list[dict[str, object]]:
        cases: list[dict[str, object]] = []
        limiter = _make_limiter(request.timeout_seconds)
        for case in request.test_cases:
            stdin = str(case.payload.get("input", ""))
            expected = normalize_output(str(case.payload.get("expected", "")))
            entry: dict[str, object] = {
                "name": case.name,
                "weight": case.weight,
                "hidden": case.hidden,
            }
            try:
                completed = subprocess.run(
                    [str(binary)],
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout_seconds,
                    cwd=cwd,
                    preexec_fn=limiter,
                )
            except subprocess.TimeoutExpired:
                entry |= {"passed": False, "reason": "timeout"}
                cases.append(entry)
                continue

            actual = normalize_output(completed.stdout)
            passed = completed.returncode == 0 and actual == expected
            entry |= {
                "passed": passed,
                "exit_code": completed.returncode,
                "expected": expected,
                "actual": actual,
            }
            if completed.returncode < 0:
                # シグナルで殺された。CPU 上限（SIGXCPU）ならタイムアウトと同義。
                signal_number = -completed.returncode
                name = signal.Signals(signal_number).name
                entry["signal"] = name
                entry["reason"] = (
                    "timeout" if name in ("SIGXCPU", "SIGKILL") else f"killed by {name}"
                )
            elif completed.returncode != 0:
                entry["reason"] = "nonzero exit"
                entry["stderr"] = completed.stderr.strip()[:2000]
            elif not passed:
                entry["reason"] = "output mismatch"
            cases.append(entry)
        return cases

    def _level_for(self, criterion, ratio: float) -> int:
        """達成率に対応するルーブリック段階を選ぶ（超えた中で最も高い段階）。"""
        best = criterion.levels[0]
        for level in criterion.levels:
            if ratio + 1e-9 >= level.score_ratio:
                best = level
        return best.level

    def _rationale(self, passed: int, total: int, failed: list[dict[str, object]]) -> str:
        if not failed:
            return f"テストケース {total} 件すべてに正しい出力を返しました。"
        names = ", ".join(str(case["name"]) for case in failed[:5])
        suffix = " ほか" if len(failed) > 5 else ""
        return f"テストケース {total} 件中 {passed} 件が一致しました。不一致: {names}{suffix}。"

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
        note: str | None,
    ) -> CriterionScore:
        return CriterionScore(
            id=CriterionScoreId(new_id("cs")),
            criterion_id=criterion_id,
            # パイプラインが EvaluatorResult 採番後に貼り替える。
            evaluator_result_id=EvaluatorResultId(new_id("evr")),
            kind=EvaluatorKind.DETERMINISTIC,
            level=level,
            score_ratio=ratio,
            weight=weight,
            confidence=1.0,
            conclusive=True,
            evidence=(
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=WholeSpan(),
                    note=note,
                ),
            ),
            rationale=rationale,
        )


def build() -> CodeTestRunner:
    """entry point から呼ばれるファクトリ。"""
    return CodeTestRunner()
