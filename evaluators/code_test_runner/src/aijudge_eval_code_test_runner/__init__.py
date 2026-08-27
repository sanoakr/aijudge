"""C プログラムをテストケースで採点する決定的評価器。

Sharif Judge の中核機能に相当する。標準入力を与えて標準出力を比較する。

実行はすべて S4（`aijudge_sandbox`）を通す。この評価器は subprocess を
直接呼ばない。呼べるようにしておくと、どこか一箇所で隔離が抜け、
抜けたことに気づく方法も無くなる（ADR 0006）。

隔離手段が無い環境では、採点を諦めて失敗として返す。0 点にもしない
（隔離できないのは学習者の責任ではない）。隔離なしで点が付いてしまうより、
点が付かない方がましだから。

判定は決定的（`conclusive=True`）。ここで不正解が確定した観点は
AI 評価器に問い合わせず、AI の判断で覆されることもない（設計原則 P3）。
"""

from __future__ import annotations

import math

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
    ExecResult,
    Limits,
    Sandbox,
    SandboxError,
    Workspace,
    default_sandbox,
)

EVALUATOR_ID = "code_test_runner"

SOURCE_NAME = "main.c"
BINARY_NAME = "main"

_MEMORY_BYTES = 512 * 1024 * 1024
_MAX_PROCESSES = 64
_MAX_OUTPUT_BYTES = 1024 * 1024


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


def _limits(timeout_seconds: float) -> Limits:
    return Limits(
        cpu_seconds=max(1, math.ceil(timeout_seconds)),
        wall_seconds=timeout_seconds,
        memory_bytes=_MEMORY_BYTES,
        processes=_MAX_PROCESSES,
        output_bytes=_MAX_OUTPUT_BYTES,
    )


class CodeTestRunner:
    """コンパイルして実行し、期待出力と比較する。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def __init__(self, sandbox: Sandbox | None = None, *, compiler: str = "cc") -> None:
        # サンドボックスの用意は最初の採点まで遅らせる。起動時に
        # Docker が無いだけでプロセス全体が上がらないのは行き過ぎ。
        self._sandbox = sandbox
        self._compiler = compiler

    def _resolve_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = default_sandbox()
        return self._sandbox

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
        if source is None or source_id is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error="no source artifact found in the submission",
            )

        limits = _limits(request.timeout_seconds)
        try:
            sandbox = self._resolve_sandbox()
            with sandbox.workspace() as workspace:
                workspace.write(SOURCE_NAME, source)
                compiled = self._compile(workspace, limits)
                if not compiled.ok:
                    return self._compile_failure(request, criterion, source_id, compiled)
                cases = self._run_cases(request, workspace, limits)
        except SandboxError as exc:
            # 隔離できないなら採点しない。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"cannot execute the submission safely: {exc}",
            )

        passed_weight = sum(case["weight"] for case in cases if case["passed"])
        total_weight = sum(case["weight"] for case in cases) or 1.0
        ratio = passed_weight / total_weight
        failed = [case for case in cases if not case["passed"]]

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
                    rationale=self._rationale(len(cases) - len(failed), len(cases), failed),
                    note=None,
                ),
            ),
            raw_output={
                "cases": cases,
                # どの隔離で走らせたかを残す。後から監査できないと意味がない。
                "sandbox": sandbox.name,
                "isolation": sandbox.isolation.value,
            },
        )

    # -- internals ---------------------------------------------------------

    def _compile(self, workspace: Workspace, limits: Limits) -> ExecResult:
        return workspace.run(
            ExecRequest(
                argv=(self._compiler, "-std=c11", "-O0", "-o", BINARY_NAME, SOURCE_NAME),
                limits=limits,
                # コンパイラは信頼できる実行体。中間ファイルの置き場が要る。
                # 提出物そのものを動かすときは決して立てない。
                trusted_toolchain=True,
            )
        )

    def _compile_failure(
        self,
        request: EvaluationRequest,
        criterion,
        source_id: ArtifactId,
        result: ExecResult,
    ) -> EvaluationOutcome:
        """コンパイルエラーは 0 点で確定であって、評価器の失敗ではない。"""
        detail = (result.stderr or result.stdout).strip()[:2000]
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
                    note=detail,
                ),
            ),
            raw_output={"compile_error": detail, "timed_out": result.timed_out},
        )

    def _target_criterion(self, request: EvaluationRequest):
        """この評価器が担当する観点を選ぶ。

        観点側の `evaluator_id` 指定を優先し、無ければ最初の未割当を採る。
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
        self, request: EvaluationRequest, workspace: Workspace, limits: Limits
    ) -> list[dict[str, object]]:
        cases: list[dict[str, object]] = []
        for case in request.test_cases:
            expected = normalize_output(str(case.payload.get("expected", "")))
            entry: dict[str, object] = {
                "name": case.name,
                "weight": case.weight,
                "hidden": case.hidden,
            }
            result = workspace.run(
                ExecRequest(
                    argv=(f"./{BINARY_NAME}",),
                    stdin=str(case.payload.get("input", "")),
                    limits=limits,
                )
            )

            if result.timed_out:
                entry |= {"passed": False, "reason": "timeout"}
                cases.append(entry)
                continue

            actual = normalize_output(result.stdout)
            passed = result.exit_code == 0 and actual == expected
            entry |= {
                "passed": passed,
                "exit_code": result.exit_code,
                "expected": expected,
                "actual": actual,
            }
            if result.signal_name:
                entry["signal"] = result.signal_name
                entry["reason"] = f"killed by {result.signal_name}"
            elif result.exit_code != 0:
                entry["reason"] = "nonzero exit"
                entry["stderr"] = result.stderr.strip()[:2000]
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
