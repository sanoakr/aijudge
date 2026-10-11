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

## 関数の検査（`c_function_check`・2026-10-11）

同じパッケージに 2 つ目の評価器を置く（`CFunctionCheck`）。課題が「指定の関数を
定義すること」を求めるときのためにある ── 入出力だけを見ると、関数を使わずに
`main()` へ全部書いた解も満点になる。コンパイルと実行の部品はこちらと共有する
（違うのはソースのつなぎ方だけで、伴走プロセスを立てる `network_test_runner` の
ような実行の仕方の違いは無い）。

**評価器を分けたのは、検証データを入出力のケースと混ぜないため。** 同じ評価器の
ケースに印を付けて混ぜると、入出力の編集欄（コンソール）や IDE のテスト実行が
それを入出力として扱い、保存のたびに検査用のソースが落ちる。評価器が違えば、
画面は形（`test_case_shape`）で見分け、IDE は自分あてのケースしか読まない。
"""

from __future__ import annotations

import math
import re

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    TestCase,
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
    "DRIVER_KEY",
    "EVALUATOR_ID",
    "FUNCTION_CHECK_ID",
    "LANGUAGES",
    "OPTION_CASE_TIMEOUT",
    "OPTION_COMPILE_TIMEOUT",
    "CFunctionCheck",
    "CodeTestRunner",
    "Language",
    "UnknownLanguage",
    "build",
    "build_function_check",
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
    # 入出力セット（`EvaluationRequest.test_cases`）を読む（#300）。
    # **画面はこの宣言で欄を出す** ── 評価器名の表を画面に持たせると、
    # 評価器を足した日にその表だけが古くなる。
    uses_test_cases = True

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
                cases = self._run_cases(list(request.test_cases), workspace, language, case_limits)
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
        explicit = [c for c in request.task_version.criteria if c.evaluator_id == self.evaluator_id]
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
        test_cases: list[TestCase],
        workspace: Workspace,
        language: Language,
        limits: Limits,
    ) -> list[dict[str, object]]:
        cases: list[dict[str, object]] = []
        for case in test_cases:
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


# 提出の `main` に付ける別名。検査用の `main()` と衝突させないため。学習者が
# 書きそうにない名前にしてある。
_STUDENT_MAIN = "aijudge_student_main"


def _with_driver(source: bytes, driver: str) -> bytes:
    """提出のソースの後ろに検査用のソースをつなぐ。

    `#define main` で提出の `main` を別名にし、`#undef` してから検査用の
    `main()` を置く。`#line` はエラーの行番号を学習者のファイルに合わせるため
    （つないだ分だけずれると、自分のコードのどこが悪いのか読めない）。
    """
    text = source.decode("utf-8", errors="replace")
    if not text.endswith("\n"):
        text += "\n"
    return (
        f'#define main {_STUDENT_MAIN}\n#line 1 "main.c"\n{text}'
        f'#undef main\n#line 1 "function_check.c"\n{driver}'
    ).encode()


def _check_entry(
    case: TestCase, *, passed: bool, reason: str, detail: str | None = None
) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": case.name,
        "weight": case.weight,
        "hidden": case.hidden,
        "passed": passed,
        "reason": reason,
    }
    if detail is not None:
        entry["compile_error"] = detail
    return entry


# 未定義の関数を指すリンカのメッセージ。GNU ld（運用のコンテナ）と
# Apple ld（手元の macOS）の両方。
_UNDEFINED = (
    re.compile(r"undefined reference to [`'\"]?(?P<name>\w+)"),
    re.compile(r'"_(?P<name>\w+)", referenced from'),
)
_CONFLICTING = re.compile(r"conflicting types for [`'\"‘]?(?P<name>\w+)")


def _function_check_rationale(failed: list[dict[str, object]]) -> str:
    """関数の検査に落ちた理由を、学習者が直せる言い方で返す。"""
    head = "指定された関数の検査に通りませんでした。"
    for case in failed:
        detail = str(case.get("compile_error", ""))
        if not detail:
            continue
        for pattern in _UNDEFINED:
            if match := pattern.search(detail):
                return head + f"関数 {match.group('name')}() が定義されていません。"
        if match := _CONFLICTING.search(detail):
            return head + f"関数 {match.group('name')}() の引数か返り値の型が課題の指定と違います。"
        excerpt = _compile_excerpt(detail)
        return head + (f"{excerpt} " if excerpt else "")
    if any(case.get("reason") == "timeout" for case in failed):
        return head + "関数の呼び出しが時間内に終わりませんでした。"
    return head + "関数の結果が期待と違いました。"


FUNCTION_CHECK_ID = "c_function_check"
#: 検査用のソース（`main()` を含む）を持つ `payload` のキー。
DRIVER_KEY = "driver"


class CFunctionCheck(CodeTestRunner):
    """指定の関数が定義されているかを、検査用の `main()` から呼んで確かめる。

    提出のソースの `main` を別名に置き換え、その後ろに教員の検査用 `main()` を
    つないで **1 本の翻訳単位として**コンパイルし、出力を期待出力と比べる。

    - 関数が無ければリンクで、引数や返り値の型が違えばコンパイルで落ちる。
      1 本にしたのはこのため ── 別々にコンパイルしてリンクすると型の違いが
      通り、未定義動作のまま値を比べることになる。
    - **1 件でも通らなければ 0%、全部通れば 100%。** 部分点は付けない ──
      関数があるかどうかを問う観点で、AND の先頭に置けば「関数が無ければ
      0 点」（2025 年度の扱い）になる。
    - 提出がそもそもコンパイルできなければ 0%（正しさの観点と同じ扱い）。
    """

    evaluator_id = FUNCTION_CHECK_ID
    # 入出力の形ではない（検査用のソースを持つ）。**画面に入出力の欄で編集させない**
    # ── させると保存のたびに検査用のソースが落ちる。知らない形は編集欄を出さない。
    test_case_shape = "function"

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = self._target_criterion(request)
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion is assigned to this evaluator"},
            )
        if not request.test_cases:
            # 検査が無いのに観点だけある。**満点にしない**（確かめていない）。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no function checks"},
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
        if language.compile_argv is None or not language.source_name.endswith(".c"):
            # つなぎ方（`main` の置き換え）は C のもの。他の言語で黙って通すと、
            # 検査していないのに検査済みに見える。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"{FUNCTION_CHECK_ID} supports C only, not {language.label}",
            )

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
            sandbox = self._resolve_sandbox(request.options)
            with sandbox.workspace() as workspace:
                # 提出そのものがコンパイルできるかを先に見る。できないのに検査の
                # 失敗として「関数が定義されていません」と言うと、直す先を誤らせる。
                workspace.write(language.source_name, source)
                compiled = self._compile(workspace, language, compile_limits)
                if compiled is not None and not compiled.ok:
                    return self._compile_failure(request, criterion, source_id, compiled)
                checked = self._run_checks(
                    list(request.test_cases),
                    source,
                    workspace,
                    language,
                    compile_limits,
                    case_limits,
                )
        except SandboxError as exc:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"cannot execute the submission safely: {exc}",
            )

        failed = [case for case in checked if not case["passed"]]
        ratio = 0.0 if failed else 1.0
        rationale = (
            _function_check_rationale(failed)
            if failed
            else f"指定された関数の検査 {len(checked)} 件に通りました。"
        )
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
                    rationale=rationale,
                    note=None,
                ),
            ),
            raw_output={
                "cases": checked,
                "sandbox": sandbox.name,
                "isolation": sandbox.isolation.value,
            },
        )

    def _target_criterion(self, request: EvaluationRequest):
        """観点が名指ししたときだけ担当する。

        **未割当の観点を拾わない。** 拾うと、関数の検査を持たない課題で
        正しさの観点を横取りしうる。
        """
        explicit = [c for c in request.task_version.criteria if c.evaluator_id == self.evaluator_id]
        return explicit[0] if explicit else None

    def _run_checks(
        self,
        checks: list[TestCase],
        source: bytes,
        workspace: Workspace,
        language: Language,
        compile_limits: Limits,
        case_limits: Limits,
    ) -> list[dict[str, object]]:
        """検査用のソースごとに 1 回コンパイルし、そのケースを走らせる。

        コンパイルやリンクに失敗したら、そのソースのケースはすべて不合格。
        """
        results: list[dict[str, object]] = []
        drivers: dict[str, list[TestCase]] = {}
        for case in checks:
            drivers.setdefault(str(case.payload.get(DRIVER_KEY, "")), []).append(case)
        for driver, group in drivers.items():
            if not driver.strip():
                results += [
                    _check_entry(case, passed=False, reason="no driver source") for case in group
                ]
                continue
            workspace.write(language.source_name, _with_driver(source, driver))
            compiled = self._compile(workspace, language, compile_limits)
            if compiled is not None and not compiled.ok:
                detail = (compiled.stderr or compiled.stdout).strip()[:2000]
                results += [
                    _check_entry(case, passed=False, reason="compile failed", detail=detail)
                    for case in group
                ]
                continue
            results += self._run_cases(group, workspace, language, case_limits)
        return results


def build() -> CodeTestRunner:
    """entry point から呼ばれるファクトリ。"""
    return CodeTestRunner()


def build_function_check() -> CFunctionCheck:
    """`c_function_check` の entry point から呼ばれるファクトリ。"""
    return CFunctionCheck()
