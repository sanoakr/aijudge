"""提出が、問題文のテンプレートの**指定の範囲の外**を変えていないかを決定的に判定する評価器。

## なぜ AI に訊かないか

「`// ここから` と `// ここまで` の間以外を変えていないか」は、テンプレートと
提出を突き合わせれば決まる（P3）。2026-11-12 の prog2 exam08 p1 では、この観点を
`rubric_ai_judge` に任せたところ、154 件中 25 件で 3 回の判定が割れてレビューに
回った。比べれば分かることを多数決にかけていた。

## テンプレートはどこから取るか

**問題文（`TaskVersion.statement`）のコードブロックのうち、開始の目印を含む
最初のもの**。学習者が読んだテンプレートと採点が比べるテンプレートを同じ
ものにするため、別の場所に写しを持たない ── 写しは問題文の訂正に追随しない。
見つからなければ `FAILED` にする。黙って満点や 0 点にすると設定の誤りが成績に
出るが、失敗なら観点が未採点のままレビューに回り、教員の目に入る。

## 何を「変えていない」とみなすか

- **空白は問わない。** 字下げ・空行・演算子の前後の空白の違いは変更に数えない。
  比べる前に、文字列・文字の定数の外にある空白をすべて取り除く（文字列の中の
  空白は出力を変えるので残す）。旧観点の判定の注意書き（「空行や字下げのわずかな
  違いは問わない」）を引き継いだ。
- **目印の 2 行は消してよい。** 範囲の中身は何を書いてもよい（行を足しても
  よい）。
- **目印が残っていれば、範囲は目印の内側である。** 開始の目印より前、終了の
  目印より後ろに行を足せば、範囲の外を変えたことになる。
- **それ以外の行（コメントを含む）は、テンプレートと同じ順で全部そろって
  いなければならない。** 足しても消しても変えても「変えている」。

範囲が複数あるテンプレートにも使える（固定部分と範囲が交互に並ぶ）。

## 全体比較（`whole_program`・2026-11-14）

「コメントだけを書き換え、コードは一切変えない」課題（prog2 exam08 p2: 選択ソートの
各段にコメントを書く）には目印が無い ── プログラム全体が「変えてはいけない部分」で、
書いてよいのはコメントだけである。そこで**コメントと空白を除いた提出全体**を、問題文の
コードブロック（言語名の付いた最初のもの。無ければ最初のもの）と比べるモードを足した。

- 課題ごとに宣言する: この評価器あての検証データ（`evaluator: template_diff_check`）の
  payload に `whole_program: true`。科目プロファイルの `evaluator_options` でも立てられるが、
  科目の全課題に効くので、目印を使う課題と同じ科目では課題の側に書く。
- コメント（`//` と `/* */`）は中身ごと除く。文字列・文字の定数の中は残す（`"%d "` の
  空白も出力を変える）。空白の扱いは目印のモードと同じ（問わない）。
- 以前は exam08 p2 のこの判定を AI に任せていた（判定の注意書きの「コードの行を変えて
  いれば 1 段下げる」）。比べれば決まることなので、ここに移す（P3）。

## 段階

満たせば最上段、満たさなければ最下段。`conclusive` を立てる ── 比べた結果に
迷いは無い（P3）。根拠には、最初に食い違った行と、テンプレートのその位置の行を
書く（教員がその場で確かめられるように）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    LineSpan,
    RubricCriterion,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "template_diff_check"

#: 範囲の目印の既定値。科目プロファイルの evaluator_options（`start_marker` /
#: `end_marker`）で変えられる（Python なら `# ここから` など）。
DEFAULT_START_MARKER = "// ここから"
DEFAULT_END_MARKER = "// ここまで"

#: 根拠の引用の上限（字）。
MAX_QUOTE_CHARS = 500

#: 問題文のコードブロック（``` で囲む。言語名は任意）。
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


# --------------------------------------------------------------------------
# 正規化
# --------------------------------------------------------------------------


def normalize_line(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """1 行から、文字列・文字の定数の外にある空白を取り除く。

    コメントの中に `'` や `"` があっても定数の始まりと読まないよう、`//` と
    `/* */` を追う（ブロックコメントは行をまたぐので状態を返す）。コメントの
    中身は残す ── テンプレートのコメントも「変えてはいけない行」の一部である。
    """
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_block_comment:
            if line.startswith("*/", i):
                out.append("*/")
                i += 2
                in_block_comment = False
                continue
            if not ch.isspace():
                out.append(ch)
            i += 1
            continue
        if line.startswith("//", i):
            # 行末までコメント。空白だけ落として残す。
            out.append("".join(c for c in line[i:] if not c.isspace()))
            break
        if line.startswith("/*", i):
            out.append("/*")
            i += 2
            in_block_comment = True
            continue
        if ch in "\"'":
            # 定数は終わりの引用符まで（エスケープを飛ばす）そのまま写す。
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if line[j] == "\\" else 1
            out.append(line[i : j + 1])
            i = j + 1
            continue
        if not ch.isspace():
            out.append(ch)
        i += 1
    return "".join(out), in_block_comment


@dataclass(frozen=True)
class Line:
    """正規化した 1 行と、元の行番号（1 始まり）・元の文字列。"""

    text: str
    number: int
    raw: str


def normalized_lines(source: str) -> tuple[Line, ...]:
    """空行（正規化して空になる行）を除いた行の並び。"""
    lines: list[Line] = []
    in_block = False
    for number, raw in enumerate(source.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        text, in_block = normalize_line(raw, in_block)
        if text:
            lines.append(Line(text=text, number=number, raw=raw.rstrip()))
    return tuple(lines)


def strip_comment_line(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """1 行から、コメントを中身ごと除き、文字列・文字の定数の外にある空白を取り除く。

    `normalize_line` との違いはコメントを残さないことだけ（全体比較のモードで使う）。
    """
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_block_comment:
            if line.startswith("*/", i):
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if line.startswith("//", i):
            break
        if line.startswith("/*", i):
            i += 2
            in_block_comment = True
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if line[j] == "\\" else 1
            out.append(line[i : j + 1])
            i = j + 1
            continue
        if not ch.isspace():
            out.append(ch)
        i += 1
    return "".join(out), in_block_comment


def code_lines(source: str) -> tuple[Line, ...]:
    """コメントと空白を除いて空にならない行の並び（全体比較のモード）。"""
    lines: list[Line] = []
    in_block = False
    for number, raw in enumerate(source.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        text, in_block = strip_comment_line(raw, in_block)
        if text:
            lines.append(Line(text=text, number=number, raw=raw.rstrip()))
    return tuple(lines)


#: 言語名の付いたコードブロック（入出力の例のブロックを取らないため）。
_NAMED_FENCE = re.compile(r"^```[ \t]*[A-Za-z][^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def find_program(statement: str) -> str | None:
    """問題文のプログラム。言語名の付いた最初のコードブロック（無ければ最初のもの）。"""
    named = _NAMED_FENCE.search(statement)
    if named is not None:
        return named.group(1)
    any_block = _FENCE.search(statement)
    return None if any_block is None else any_block.group(1)


def check_whole_program(source: str, program: str) -> Verdict:
    """コメントと空白を除いた提出全体が、問題文のプログラムと同じか。"""
    submitted = code_lines(source)
    expected = code_lines(program)
    for k in range(max(len(submitted), len(expected))):
        got = submitted[k] if k < len(submitted) else None
        want = expected[k] if k < len(expected) else None
        if got is None or want is None or got.text != want.text:
            return Verdict(kept=False, submitted=got, expected=want)
    return Verdict(kept=True)


def find_template(statement: str, start_marker: str) -> str | None:
    """問題文のコードブロックのうち、開始の目印の行を含む最初のもの。"""
    start = normalize_line(start_marker, False)[0]
    for match in _FENCE.finditer(statement):
        block = match.group(1)
        if any(line.text == start for line in normalized_lines(block)):
            return block
    return None


# --------------------------------------------------------------------------
# 突き合わせ
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    """固定部分（`fixed`）と範囲が交互に並ぶ。範囲は固定部分の間に 1 つずつある。"""

    fixed: tuple[tuple[Line, ...], ...]
    start: str
    end: str

    @property
    def regions(self) -> int:
        return len(self.fixed) - 1


class TemplateError(ValueError):
    """テンプレートの目印が対になっていない。"""


def split_template(template: str, start_marker: str, end_marker: str) -> Template:
    start = normalize_line(start_marker, False)[0]
    end = normalize_line(end_marker, False)[0]
    fixed: list[tuple[Line, ...]] = []
    current: list[Line] = []
    inside = False
    for line in normalized_lines(template):
        if line.text == start:
            if inside:
                raise TemplateError(f"{line.number} 行目: 開始の目印が続いています")
            fixed.append(tuple(current))
            current = []
            inside = True
        elif line.text == end:
            if not inside:
                raise TemplateError(f"{line.number} 行目: 開始の目印の無い終了の目印です")
            inside = False
        elif not inside:
            current.append(line)
    if inside:
        raise TemplateError("終了の目印がありません")
    fixed.append(tuple(current))
    if len(fixed) < 2:
        raise TemplateError("開始の目印がありません")
    return Template(fixed=tuple(fixed), start=start, end=end)


@dataclass(frozen=True)
class Verdict:
    kept: bool
    # 食い違った最初の提出の行（足した行・変えた行）。無ければ None（行が足りない）。
    submitted: Line | None = None
    # その位置にあるはずだったテンプレートの行。無ければ None（行が余分）。
    expected: Line | None = None


def _gap_ok(gap: tuple[Line, ...], template: Template) -> bool:
    """範囲 1 つ分の中身。目印が残っていれば、目印の外側に行があってはいけない。"""
    # 開始の目印があれば先頭に、終了の目印があれば末尾に来ていなければならない
    # （目印が複数あっても、最初の開始より前・最後の終了より後ろが外になる）。
    texts = [line.text for line in gap]
    starts_inside = template.start not in texts or texts[0] == template.start
    ends_inside = template.end not in texts or texts[-1] == template.end
    return starts_inside and ends_inside


def _matches(lines: tuple[Line, ...], at: int, segment: tuple[Line, ...]) -> bool:
    if at + len(segment) > len(lines):
        return False
    return all(lines[at + k].text == segment[k].text for k in range(len(segment)))


def check_template(source: str, template: Template) -> Verdict:
    """提出が、テンプレートの範囲の外を変えていないか。"""
    lines = normalized_lines(source)
    head, *middle, tail = template.fixed

    def search(index: int, at: int) -> bool:
        # index 番目の範囲の中身が at から始まる。残りの固定部分を後ろへ探す。
        if index == len(middle):
            begin = len(lines) - len(tail)
            return (
                begin >= at and _matches(lines, begin, tail) and _gap_ok(lines[at:begin], template)
            )
        segment = middle[index]
        for position in range(at, len(lines) - len(segment) + 1):
            if (
                _matches(lines, position, segment)
                and _gap_ok(lines[at:position], template)
                and search(index + 1, position + len(segment))
            ):
                return True
        return False

    if _matches(lines, 0, head) and search(0, len(head)):
        return Verdict(kept=True)
    return _first_difference(lines, template)


def _first_difference(lines: tuple[Line, ...], template: Template) -> Verdict:
    """根拠に書く食い違い。先頭の固定部分、次に末尾の固定部分から探す。"""
    head, tail = template.fixed[0], template.fixed[-1]
    for k, expected in enumerate(head):
        if k >= len(lines):
            return Verdict(kept=False, expected=expected)
        if lines[k].text != expected.text:
            return Verdict(kept=False, submitted=lines[k], expected=expected)
    for k, expected in enumerate(reversed(tail), 1):
        if k > len(lines) - len(head):
            return Verdict(kept=False, expected=expected)
        if lines[-k].text != expected.text:
            return Verdict(kept=False, submitted=lines[-k], expected=expected)
    # 先頭と末尾は合っている。中ほどの固定部分か、目印の外側に足した行が原因。
    body = lines[len(head) : len(lines) - len(tail)]
    for segment in template.fixed[1:-1]:
        for expected in segment:
            if not any(line.text == expected.text for line in body):
                return Verdict(kept=False, expected=expected)
    texts = [line.text for line in body]
    if template.start in texts and texts.index(template.start) != 0:
        return Verdict(kept=False, submitted=body[0])
    if template.end in texts:
        last = len(texts) - 1 - texts[::-1].index(template.end)
        if last != len(texts) - 1:
            return Verdict(kept=False, submitted=body[last + 1])
    return Verdict(kept=False)


# --------------------------------------------------------------------------
# 評価器
# --------------------------------------------------------------------------


def whole_program_requested(request: EvaluationRequest) -> bool:
    """全体比較のモードか。課題の検証データ（自分あて）か、評価器のオプションで立てる。"""
    if bool(request.options.get("whole_program")):
        return True
    return any(
        bool(case.payload.get("whole_program"))
        for case in request.test_cases
        if case.evaluator_id in (None, EVALUATOR_ID)
    )


def judge_level(verdict: Verdict, criterion: RubricCriterion) -> int:
    levels = sorted(level.level for level in criterion.levels)
    return levels[-1] if verdict.kept else levels[0]


class TemplateDiffCheck:
    """提出がテンプレートの指定の範囲の外を変えていないかを比べる（決定的）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC
    # 全体比較の宣言（`whole_program`）を課題の検証データから読む（2026-11-14）。
    # 画面には編集欄の無い形として出る（course.yaml で書く）。
    uses_test_cases = True
    test_case_shape = "template_mode"

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        targets = [c for c in request.task_version.criteria if c.evaluator_id == EVALUATOR_ID]
        if not targets:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion names template_diff_check"},
            )
        if whole_program_requested(request):
            return self._evaluate_whole(request, targets)
        start_marker = str(request.options.get("start_marker", DEFAULT_START_MARKER))
        end_marker = str(request.options.get("end_marker", DEFAULT_END_MARKER))
        text = find_template(request.task_version.statement, start_marker)
        if text is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"問題文に「{start_marker}」を含むコードブロック（テンプレート）がありません",
            )
        try:
            template = split_template(text, start_marker, end_marker)
        except TemplateError as exc:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED, error=f"問題文のテンプレートが不正です: {exc}"
            )
        found = self._source(request)
        if found is None:
            # ソースが無い・読めない。**0 点にしない** ── 出し忘れか読めなかった
            # のかはここでは分からない（`c_indent_check` と同じ判断）。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED, raw_output={"reason": "no source to compare"}
            )
        artifact_id, content_hash, source = found
        verdict = check_template(source, template)
        evidence = self._evidence(artifact_id, content_hash, verdict)
        rationale = self._rationale(verdict, template)
        scores = []
        for criterion in targets:
            level = judge_level(verdict, criterion)
            scores.append(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.DETERMINISTIC,
                    level=level,
                    score_ratio=criterion.level_for(level).score_ratio,
                    weight=criterion.weight,
                    confidence=1.0,
                    # **確定させる。** 比べた結果に迷いは無い（P3）。
                    conclusive=True,
                    evidence=evidence,
                    rationale=rationale,
                )
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=tuple(scores),
            raw_output={
                "kept": verdict.kept,
                "regions": template.regions,
                "submitted_line": verdict.submitted.number if verdict.submitted else None,
                "expected_line": verdict.expected.number if verdict.expected else None,
            },
        )

    def _evaluate_whole(
        self, request: EvaluationRequest, targets: list[RubricCriterion]
    ) -> EvaluationOutcome:
        """全体比較のモード（`whole_program`）。"""
        program = find_program(request.task_version.statement)
        if program is None or not code_lines(program):
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error="問題文にプログラムのコードブロックがありません（全体比較）",
            )
        found = self._source(request)
        if found is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED, raw_output={"reason": "no source to compare"}
            )
        artifact_id, content_hash, source = found
        verdict = check_whole_program(source, program)
        evidence = self._evidence(artifact_id, content_hash, verdict, whole=True)
        rationale = self._whole_rationale(verdict)
        scores = []
        for criterion in targets:
            level = judge_level(verdict, criterion)
            scores.append(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.DETERMINISTIC,
                    level=level,
                    score_ratio=criterion.level_for(level).score_ratio,
                    weight=criterion.weight,
                    confidence=1.0,
                    conclusive=True,
                    evidence=evidence,
                    rationale=rationale,
                )
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=tuple(scores),
            raw_output={
                "kept": verdict.kept,
                "mode": "whole_program",
                "submitted_line": verdict.submitted.number if verdict.submitted else None,
                "expected_line": verdict.expected.number if verdict.expected else None,
            },
        )

    @staticmethod
    def _whole_rationale(verdict: Verdict) -> str:
        if verdict.kept:
            return "コメントと空白を除くと、問題文のプログラムと同じです。"
        submitted, expected = verdict.submitted, verdict.expected
        if submitted is not None and expected is not None:
            return (
                f"コメント以外を変えています。提出の {submitted.number} 行目"
                f"「{submitted.raw.strip()}」は、問題文では「{expected.raw.strip()}」です。"
            )
        if expected is not None:
            return (
                f"コメント以外を変えています。問題文の「{expected.raw.strip()}」が見つかりません。"
            )
        if submitted is not None:
            return (
                f"コメント以外を変えています。提出の {submitted.number} 行目"
                f"「{submitted.raw.strip()}」は、問題文に無い行です。"
            )
        return "コメント以外を変えています。"

    # -- internals ---------------------------------------------------------

    def _source(self, request: EvaluationRequest) -> tuple[ArtifactId, str, str] | None:
        """比べるソース。コードの提出物の最初の 1 つ（エディタの提出は 1 ファイル）。"""
        for artifact in request.submission.gradable_artifacts:
            if artifact.kind.value != "code":
                continue
            content = request.artifact_contents.get(artifact.id)
            if not content:
                continue
            try:
                return artifact.id, artifact.content_hash, content.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return None

    def _evidence(
        self, artifact_id: ArtifactId, content_hash: str, verdict: Verdict, *, whole: bool = False
    ) -> tuple[Evidence, ...]:
        if verdict.kept:
            return (
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=WholeSpan(),
                    note=(
                        "コメントと空白を除くと問題文のプログラムと一致"
                        if whole
                        else "範囲の外の行はすべてテンプレートと一致"
                    ),
                ),
            )
        line = verdict.submitted
        quote = []
        if line is not None:
            quote.append(f"提出 {line.number}: {line.raw.strip()}")
        if verdict.expected is not None:
            quote.append(f"テンプレート {verdict.expected.number}: {verdict.expected.raw.strip()}")
        return (
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=LineSpan(start_line=line.number, end_line=line.number)
                if line is not None
                else WholeSpan(),
                quote="\n".join(quote)[:MAX_QUOTE_CHARS] or None,
                note=(
                    "コメント以外で問題文のプログラムと食い違った最初の行"
                    if whole
                    else "範囲の外でテンプレートと食い違った最初の行"
                ),
            ),
        )

    def _rationale(self, verdict: Verdict, template: Template) -> str:
        span = f"「{template.start}」と「{template.end}」の間"
        if verdict.kept:
            return f"{span}の外は、テンプレートと同じです（空白の違いは数えません）。"
        submitted, expected = verdict.submitted, verdict.expected
        if submitted is not None and expected is not None:
            return (
                f"{span}の外を変えています。提出の {submitted.number} 行目"
                f"「{submitted.raw.strip()}」は、テンプレートでは"
                f"「{expected.raw.strip()}」です。"
            )
        if expected is not None:
            return (
                f"{span}の外を変えています。テンプレートの「{expected.raw.strip()}」"
                "が見つかりません。"
            )
        if submitted is not None:
            return (
                f"{span}の外を変えています。提出の {submitted.number} 行目"
                f"「{submitted.raw.strip()}」は、目印の外側に足した行です。"
            )
        return f"{span}の外を変えています。"


def build() -> TemplateDiffCheck:
    """entry point から呼ばれるファクトリ。"""
    return TemplateDiffCheck()


__all__ = [
    "DEFAULT_END_MARKER",
    "DEFAULT_START_MARKER",
    "EVALUATOR_ID",
    "Template",
    "TemplateDiffCheck",
    "TemplateError",
    "Verdict",
    "build",
    "check_template",
    "check_whole_program",
    "code_lines",
    "find_program",
    "find_template",
    "normalize_line",
    "normalized_lines",
    "split_template",
    "strip_comment_line",
    "whole_program_requested",
]
