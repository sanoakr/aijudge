"""提出プログラムをテストケースで採点する決定的評価器。

Sharif Judge の中核機能に相当する。標準入力を与えて標準出力を比較する。

**言語を決め打ちにしない。** 科目プロファイルの
`evaluator_options.code_test_runner.language` で選ぶ（既定は C）。
プログラミング演習は C、ネットワーク演習は Python というように、科目が
違えば言語も違う。**言語を足す作業がこのファイルに閉じている**ことが
ADR 0002 の主張（科目の追加でコアと採点エンジンが変わらない）の実際の
検証になる。

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
import re

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
from aijudge_toolchain import (
    LANGUAGES,
    OPTION_IMAGE,
    Language,
    UnknownLanguage,
    resolve_language,
)

EVALUATOR_ID = "code_test_runner"

# 言語の扱い方は `aijudge_toolchain` にある（伴走プロセスの評価器と共有する）。
# ここから再輸出するのは、既存の import 経路を壊さないため。
__all__ = [
    "DEFAULT_CASE_TIMEOUT_SECONDS",
    "DEFAULT_COMPILE_TIMEOUT_SECONDS",
    "EVALUATOR_ID",
    "LANGUAGES",
    "OPTION_CASE_TIMEOUT",
    "OPTION_COMPILE_TIMEOUT",
    "CodeTestRunner",
    "Language",
    "UnknownLanguage",
    "build",
    "format_only_mismatch",
    "normalize_output",
    "resolve_language",
]


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


# 区切りとみなす文字。空白とコンマだけにしてある ── 「値は合っているか」を
# 見るためのもので、これ以上増やすと別々の出力が同じに見え始める。
_SEPARATORS = re.compile(r"[,\s]+")


def _tokens(lines: list[str]) -> list[str]:
    """行の並びを、区切りを問わない値の並びにする。"""
    return [token for line in lines for token in _SEPARATORS.split(line.strip()) if token]


def _as_numbers(tokens: list[str]) -> list[float] | None:
    try:
        return [float(token) for token in tokens]
    except ValueError:
        return None


def format_only_mismatch(expected: list[str], actual: list[str]) -> str | None:
    """**値は合っていて書き方だけが違う**なら、その説明。違えば None。

    判定そのものは変えない ── 書式指定も課題の一部で、そこは
    `normalize_output` の判断のままである。ここで足すのは**なぜ落ちたかを
    言えるようにすること**だけ。

    言えないと何が起きるか。書式の食い違いは全ケースを同時に落とすので、
    学習者には「5 件中 0 件が一致しました」しか返らない。**解けていない
    提出と見分けが付かない。** `_common_error` が全件共通の理由を伝えようと
    しているのと同じ意図だが、あちらは stderr かシグナルがある場合
    （クラッシュ・構文エラー）しか働かず、正常終了する書式違いは漏れる。
    """
    if expected == actual:
        return None

    wanted, given = _tokens(expected), _tokens(actual)
    if wanted and wanted == given:
        if len(expected) != len(actual):
            return "出力の値は合っていますが、改行の位置が課題文の指定と違います。"
        return "出力の値は合っていますが、区切り方が課題文の指定と違います。"

    left, right = _as_numbers(wanted), _as_numbers(given)
    if left is not None and right is not None and len(left) == len(right) and left == right:
        return (
            "出力は数値として同じですが、書き方（桁数や小数点以下の表示）が課題文の指定と違います。"
        )
    return None


# テストケース 1 件あたりの実行上限（秒）。科目プロファイルの
# `evaluator_options.code_test_runner.case_timeout_seconds` で上書きできる。
#
# **科目プロファイルの `timeout_seconds` を使ってはならない。** あちらは
# 評価器 1 回の呼び出しに対する予算で、AI 評価器では LLM の応答待ち
# （ローカルモデルで 20〜120 秒）を含む。同じ値をテストケースの実行上限に
# 使うと、LLM のために伸ばした値がそのまま暴走コードの猶予になる。
#
# 実測（2026-08-28）: `timeout_seconds: 120` の科目で無限ループの提出を通したら、
# 1 ケース 120 秒 × 5 ケースでワーカーが 10 分占有された。締切前に数件あれば
# 待ち行列が止まり、§9.1 の「結果表示まで p95 < 30 秒」も満たせない。
DEFAULT_CASE_TIMEOUT_SECONDS = 5.0
OPTION_CASE_TIMEOUT = "case_timeout_seconds"

# コンパイルの上限。実行より長く取る（最適化なしでも数秒かかる課題がある）。
DEFAULT_COMPILE_TIMEOUT_SECONDS = 30.0
OPTION_COMPILE_TIMEOUT = "compile_timeout_seconds"


# 資源上限。提出物 1 プロセスに与える。伴走プロセスを使う課題は
# 別の評価器（network_test_runner）が扱うので、ここは 1 プロセス前提でよい。
_MEMORY_BYTES = 512 * 1024 * 1024
_MAX_PROCESSES = 64
_MAX_OUTPUT_BYTES = 1024 * 1024


def _limits(timeout_seconds: float) -> Limits:
    return Limits(
        cpu_seconds=max(1, math.ceil(timeout_seconds)),
        wall_seconds=timeout_seconds,
        memory_bytes=_MEMORY_BYTES,
        processes=_MAX_PROCESSES,
        output_bytes=_MAX_OUTPUT_BYTES,
    )


# 根拠に載せるエラーの長さ。全文は raw_output に残る。
_ERROR_EXCERPT_CHARS = 300


def _compile_excerpt(text: str) -> str:
    """コンパイラの出力から要点を 1 行取り出す。

    `cc` は `main.c:2:24: error: ...` の形で出す。最初の `error` 行が
    学習者にとっての要点で、それ以降は波及したものが多い。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in lines:
        if "error" in line.lower():
            return line[:_ERROR_EXCERPT_CHARS]
    return lines[0][:_ERROR_EXCERPT_CHARS]


def _common_error(failed: list[dict[str, object]]) -> str | None:
    """全ケースが共有しているエラーの要点。無ければ None。

    Python の traceback は最後の行に型と説明が出る。C の異常終了は
    シグナル名しか無い。どちらも「全件同じ理由で落ちた」ことを伝えたい。
    """
    signals = {case.get("signal") for case in failed}
    if len(signals) == 1 and (signal_name := next(iter(signals))) is not None:
        return f"{signal_name} で強制終了しました"

    messages: set[str] = set()
    for case in failed:
        text = str(case.get("stderr", "")).strip()
        if not text:
            return None
        # traceback の最終行が型と説明。それ以外の言語では最後の出力行。
        messages.add(text.splitlines()[-1].strip())
    if len(messages) != 1:
        return None
    return next(iter(messages))[:_ERROR_EXCERPT_CHARS]


def _seconds_option(options: dict[str, object], key: str, default: float) -> float:
    """科目プロファイルからの上限を読む。不正な値は既定に落とす。

    設定ミスで採点が止まるより、既定で動いた方がよい（値の妥当性は
    プロファイルのレビューで見る）。
    """
    raw = options.get(key, default)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class CodeTestRunner:
    """コンパイルして実行し、期待出力と比較する。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        # サンドボックスの用意は最初の採点まで遅らせる。起動時に
        # コンテナ実行環境が無いだけでプロセス全体が上がらないのは行き過ぎ。
        self._sandbox = sandbox
        self._explicit_sandbox = sandbox is not None
        self._by_image: dict[str, Sandbox] = {}

    def _resolve_sandbox(self, options: dict[str, object]) -> Sandbox:
        """このリクエストで使うサンドボックス。

        科目プロファイルがイメージを指定していれば、それごとに 1 つ持つ。
        言語の処理系はイメージが持つので、Python 3.13 を要求する科目と
        C を要求する科目が別のイメージを使える。指定が無ければ自動選択。
        """
        if self._explicit_sandbox and self._sandbox is not None:
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

        # 実行とコンパイルの上限は、評価器自身の設定から取る。
        # 呼び出し全体の予算（`request.timeout_seconds`）を超えないように
        # 抑えるが、**あちらを実行上限として使わない**（上の注記を参照）。
        budget = request.timeout_seconds
        case_limits = _limits(
            min(
                budget,
                _seconds_option(request.options, OPTION_CASE_TIMEOUT, DEFAULT_CASE_TIMEOUT_SECONDS),
            )
        )
        compile_limits = _limits(
            min(
                budget,
                _seconds_option(
                    request.options, OPTION_COMPILE_TIMEOUT, DEFAULT_COMPILE_TIMEOUT_SECONDS
                ),
            )
        )
        try:
            language = resolve_language(request.options)
        except UnknownLanguage as exc:
            # 既定に落とさない。落とすと言語違いが「全員 0 点」として現れる。
            return EvaluationOutcome(status=EvaluatorStatus.FAILED, error=str(exc))

        try:
            sandbox = self._resolve_sandbox(request.options)
            with sandbox.workspace() as workspace:
                workspace.write(language.source_name, source)
                compiled = self._compile(workspace, language, compile_limits)
                if compiled is not None and not compiled.ok:
                    return self._compile_failure(request, criterion, source_id, compiled)
                cases = self._run_cases(request, workspace, language, case_limits)
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

    def _compile(
        self, workspace: Workspace, language: Language, limits: Limits
    ) -> ExecResult | None:
        """コンパイルする。スクリプト言語では None を返す（段階が存在しない）。"""
        if language.compile_argv is None:
            return None
        return workspace.run(
            ExecRequest(
                argv=language.compile_argv,
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
        """コンパイルエラーは 0 点で確定であって、評価器の失敗ではない。

        **エラーの内容を根拠に載せる。** 「コンパイルに失敗しました」だけでは
        次の一手が分からない。実測（2026-08-28）で学生 UI を見たとき、
        コンパイラの出力が `raw_output` と Evidence の note にしか無く、
        学習者の画面には出ていなかった。
        """
        detail = (result.stderr or result.stdout).strip()[:2000]
        excerpt = _compile_excerpt(detail)
        rationale = "コンパイルに失敗したため、テストを実行できませんでした。"
        if excerpt:
            rationale += f" {excerpt}"
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
                    rationale=rationale,
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
        self,
        request: EvaluationRequest,
        workspace: Workspace,
        language: Language,
        limits: Limits,
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
                    argv=language.run_argv,
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

        head = f"テストケース {total} 件中 {passed} 件が一致しました。"

        # 全件が同じエラーで落ちているなら、それを書く。
        #
        # スクリプト言語にはコンパイル段階が無いので、構文エラーが「全件の
        # 実行時失敗」として現れる。ケース名を並べるだけでは、学習者に
        # 「出力が違う」としか伝わらない。C の segfault も同じ形になる。
        common = _common_error(failed) if passed == 0 else None
        if common is not None:
            return head + f"すべて同じエラーで停止しています: {common}"

        # **書式だけが違う場合も、全件が同じ理由で落ちる。** 上の
        # `_common_error` は stderr かシグナルがある場合しか働かないので、
        # 正常終了する書式違いはそこを素通りする。言わないと、学習者には
        # 「0 件が一致」しか返らず、解けていない提出と見分けが付かない。
        shape = self._format_note(failed) if passed == 0 else None
        if shape is not None:
            return head + shape + "課題文の出力形式を確かめてください。"

        names = ", ".join(str(case["name"]) for case in failed[:5])
        suffix = " ほか" if len(failed) > 5 else ""
        return head + f"不一致: {names}{suffix}。"

    def _format_note(self, failed: list[dict[str, object]]) -> str | None:
        """全ケースが同じ書式の食い違いで落ちているなら、その説明。

        **全件でなければ言わない。** 一部だけなら書式以外の誤りもあるので、
        書式のせいだと伝えると直す先を取り違えさせる。
        """
        notes = set()
        for case in failed:
            expected = case.get("expected")
            actual = case.get("actual")
            if not isinstance(expected, list) or not isinstance(actual, list):
                return None
            note = format_only_mismatch(expected, actual)
            if note is None:
                return None
            notes.add(note)
        return next(iter(notes)) if len(notes) == 1 else None

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
