"""C のソースが入れ子の深さどおりに字下げされているかを**決定的に**判定する評価器。

## 見るのは字下げだけ

名前の付け方・空行・1 行の長さ・波括弧の置き方（行末か次の行か）は見ない。
問うのは「各行の字下げが、その行の入れ子の深さ × 字下げの単位になっているか」
だけである（2026-10-11、prog2 第 4 回以降の観点）。読みやすさ全般は AI 評価器の
観点（`readability`）の仕事で、ここに混ぜると何で減点されたのか言えなくなる。

## AI を呼ばない

深さは `{ }` の入れ子から数えられ、字下げは数えれば分かる（P3）。LLM に
訊くと同じ提出に違う点が付きうるし、訊く必要が無い。

## 字下げの単位はファイルごとに推定する

2・3・4・8 スペースのどれか、またはタブ 1 つ。**どの単位で書いたかは問わない**
── ファイル全体が 3 スペースで揃っていれば満点である。推定は「深さ × 単位に
一致する行がいちばん多い単位」（同数なら 4・2・3・8 の順）。同じ深さなのに
3 文字の行と 4 文字の行が混ざっていれば、推定した単位に合わない側が崩れになる。

タブとスペースが混ざっていれば、少ない側（1 行の中で混ざっている行も）を
崩れとして数える ── エディタのタブ幅しだいで見え方が変わるため。

## 深さの数え方

- `{` で 1 段深く、`}` で戻る。`}` で始まる行は、対応する `{` の行と同じ深さ
- 波括弧を省いた `if (…)` / `for (…)` / `while (…)` / `else` / `do` の次の 1 文は
  1 段深い（連なれば重ねる）。次の行が `{` なら（括弧を次の行に置く書き方）、
  その `{` は制御文と同じ深さ
- `switch` の中の `case` / `default` は、`switch` と同じ深さでも 1 段深くてもよい。
  どちらの流儀かは最初のラベルで決め、その `switch` の中では揃っていることを求める。
  ラベルの下の文はラベルより 1 段深い

## 数えない行

空行、プリプロセッサ行（`#include` など・`\\` で続く行も）、コメントだけの行、
`/* */` の中、括弧の中で折り返した行と、`;` `{` `}` `:` で終わらない行の次の行
（式の折り返し）。どれも字下げの流儀が分かれる、または入れ子の深さと関係が無い。

## 段階

崩れた行の割合で決める。崩れが 0 行なら最上段、`partial_max_ratio`
（既定 20%）以下なら中段、それを超えれば最下段。2 段の観点では 0 行なら上・
それ以外は下。閾値は科目プロファイルの `evaluator_options.c_indent_check` で
変えられる。

`conclusive` は立てる ── 決定的評価の判定は確定である（P3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    RubricCriterion,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "c_indent_check"

#: 中段に留める崩れの割合の上限（既定）。超えれば最下段。
DEFAULT_PARTIAL_MAX_RATIO = 0.2

#: 推定する字下げの単位（スペースの数）。同数のときはこの順を優先する。
SPACE_UNITS = (4, 2, 3, 8)

#: 根拠と説明に書き出す崩れた行の数の上限。全部並べると読めない。
MAX_REPORTED_LINES = 10

#: 評価する拡張子。ファイル名が無い提出物（エディタ）は種類だけで決める。
C_SUFFIXES = (".c", ".h")

_CONTROL = re.compile(r"^(?:\}\s*)?(if|for|while|switch|else|do)\b")
_LABEL = re.compile(r"^(case\b.*|default\s*):")


@dataclass(frozen=True)
class IndentLine:
    """数えた 1 行。`number` は 1 始まり。"""

    number: int
    level: int
    indent: str
    text: str


@dataclass(frozen=True)
class IndentReport:
    """1 ファイルの判定。"""

    counted: tuple[IndentLine, ...]
    broken: tuple[IndentLine, ...]
    # 推定した単位。`"\t"` か、スペース n 個。数えた行に字下げが無ければ None。
    unit: str | None

    @property
    def ratio(self) -> float:
        return len(self.broken) / len(self.counted) if self.counted else 0.0

    @property
    def unit_label(self) -> str:
        if self.unit is None:
            return "なし"
        if self.unit == "\t":
            return "タブ"
        return f"{len(self.unit)} スペース"


@dataclass
class _Frame:
    """開いている `{` 1 つ。"""

    # `{` を含む行の深さ。`}` で始まる行はこの深さに戻る。
    open_level: int
    is_switch: bool = False
    # `{` を含む行の実際の字下げ（列）。switch のラベルの流儀を決めるのに使う。
    open_column: int = 0
    # switch の中で、ラベルの下の文を 1 段下げるか（ラベルが switch より深い流儀）。
    case_offset: int | None = None
    # この中で `else` を待っている `if` の行の深さ（内側ほど後ろ）。
    ifs: list[int] = field(default_factory=list)

    @property
    def body_level(self) -> int:
        return self.open_level + 1 + (self.case_offset or 0)


@dataclass
class _Scanner:
    """行をまたぐ字句の状態（コメント・括弧・続く行）。"""

    in_block_comment: bool = False
    in_preprocessor: bool = False
    paren_depth: int = 0
    frames: list[_Frame] = field(default_factory=list)
    # どの `{` の中でもない所で `else` を待っている `if` の深さ。
    root_ifs: list[int] = field(default_factory=list)
    # 波括弧を省いた制御文の次の文の深さ（制御文の行の深さ + 1）。None なら下げない。
    next_level: int | None = None
    pending_switch: bool = False
    # 複数行にまたがる制御文の頭の行の深さ。
    header_level: int = 0
    # 前の数えた行が文の途中で終わったか（式の折り返し）。
    continued: bool = False
    # 複数行にまたがる制御文の頭（`for (i = 0;` で折り返した）。
    header_open: bool = False


def code_part(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """コメントと文字列・文字定数の中身を落とした行と、行末でのコメントの状態。

    文字列の中の `{` やコメントの中の `}` を深さに数えないため。文字列は
    `""` に潰して残す（行末が `;` かどうかは見たいので）。
    """
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if in_block_comment:
            end = line.find("*/", i)
            if end < 0:
                return "".join(out), True
            in_block_comment = False
            i = end + 2
            out.append(" ")
            continue
        ch = line[i]
        if line.startswith("//", i):
            break
        if line.startswith("/*", i):
            in_block_comment = True
            i += 2
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if line[j] == "\\" else 1
            out.append(ch + ch)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_block_comment


def _leading_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def check_indentation(source: str) -> IndentReport:
    """C のソース 1 本の字下げを判定する。**ここに LLM は関与しない。**"""
    scanner = _Scanner()
    lines: list[IndentLine] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        line = _classify(scanner, raw, number)
        if line is not None:
            lines.append(line)
    return _judge(tuple(lines))


def _classify(s: _Scanner, raw: str, number: int) -> IndentLine | None:
    """1 行を読み、数える行なら深さを付けて返す。状態は `s` に積む。"""
    started_in_comment = s.in_block_comment
    code, s.in_block_comment = code_part(raw, s.in_block_comment)
    stripped = code.strip()

    # プリプロセッサ行（`\` で続く行を含む）は数えない。深さにも効かせない。
    if s.in_preprocessor or (not started_in_comment and stripped.startswith("#")):
        s.in_preprocessor = raw.rstrip().endswith("\\")
        return None
    if not stripped:
        # 空行・コメントだけの行・コメントの中。
        return None

    skip = s.paren_depth > 0 or s.continued
    indent = _leading_indent(raw)

    # 行頭の `}` を先に閉じる。その行は対応する `{` の行と同じ深さ。
    level: int | None = None
    rest = stripped
    closed = False
    while rest.startswith("}"):
        level = s.frames.pop().open_level if s.frames else 0
        s.next_level = None
        rest = rest[1:].lstrip()
        closed = True

    top = s.frames[-1] if s.frames else None
    ifs = top.ifs if top is not None else s.root_ifs
    header = _CONTROL.match(rest)
    keyword = header.group(1) if header else ""
    if level is None:
        base = top.body_level if top else 0
        if rest.startswith("{") and s.next_level is not None:
            # 括弧を次の行に置く書き方。`{` は制御文と同じ深さ。
            level = s.next_level - 1
        elif top is not None and top.is_switch and _LABEL.match(rest):
            level = _label_level(top, indent)
        elif keyword == "else" and ifs and s.next_level is None:
            # `else` は対応する `if` と同じ深さ。1 行で閉じた `if (c) x;` の後でも、
            # 外側の制御文の下げ（`for (…)` の本体など）を失わない。
            level = ifs[-1]
        else:
            level = s.next_level if s.next_level is not None else base

    # `else` を待っている `if` を整える。`else` は直近の `if` と組になって消え、
    # 文の頭（`}` や `{` で始まらない行）は、同じ深さ以上の `if` をもう `else` が
    # 来ないものとして消す。`if`（`else if` を含む）の行は自分の深さを積む。
    if keyword == "else":
        if ifs and ifs[-1] == level:
            ifs.pop()
    elif not closed and not rest.startswith("{"):
        ifs[:] = [depth for depth in ifs if depth < level]
    if keyword == "if" or re.match(r"^else\s+if\b", rest):
        ifs.append(level)

    # この行の字句を最後まで読み、括弧と波括弧を数える。
    opens_switch = s.pending_switch or keyword == "switch"
    for ch in rest:
        if ch == "(":
            s.paren_depth += 1
        elif ch == ")":
            s.paren_depth = max(0, s.paren_depth - 1)
        elif ch == "{":
            s.frames.append(
                _Frame(open_level=level, is_switch=opens_switch, open_column=_column(indent))
            )
            opens_switch = False
            s.next_level = None
            s.pending_switch = False
        elif ch == "}" and s.frames:
            s.frames.pop()

    tail = stripped[-1]
    if header:
        s.header_level = level
    if header or s.header_open:
        if s.paren_depth > 0:
            s.header_open = True
        else:
            s.header_open = False
            if tail not in ";{}":
                # 波括弧を省いた制御文。次の文は制御文の行より 1 段深い。
                s.next_level = s.header_level + 1
                s.pending_switch = s.pending_switch or keyword == "switch"
            elif tail == ";":
                # `if (x) a;` のように 1 行で閉じた。
                s.next_level = None
    elif tail == ";" and s.paren_depth == 0:
        s.next_level = None
    s.continued = s.paren_depth == 0 and not s.header_open and tail not in ";{}:)" and not header
    if skip:
        return None
    return IndentLine(number=number, level=level, indent=indent, text=raw)


def _label_level(frame: _Frame, indent: str) -> int:
    """`case` / `default` の深さ。流儀は最初のラベルで決める。"""
    if frame.case_offset is None:
        # switch の行より深く書いたか。単位はまだ推定していないので、列だけで比べる。
        frame.case_offset = 1 if _column(indent) > frame.open_column else 0
    return frame.open_level + frame.case_offset


def _column(indent: str) -> int:
    return len(indent.expandtabs(8))


def _judge(lines: tuple[IndentLine, ...]) -> IndentReport:
    """推定した単位で、深さどおりでない行を数える。"""
    indented = [line for line in lines if line.level > 0 and line.indent]
    tabs = sum(1 for line in indented if set(line.indent) == {"\t"})
    spaces = sum(1 for line in indented if set(line.indent) == {" "})
    use_tabs = tabs > spaces

    unit: str | None = None
    if indented:
        if use_tabs:
            unit = "\t"
        else:
            unit = " " * max(
                SPACE_UNITS,
                key=lambda size: (
                    sum(1 for line in indented if line.indent == " " * size * line.level),
                    -SPACE_UNITS.index(size),
                ),
            )

    # 字下げのある行が 1 つも無ければ単位は推定できない（`None`）。そのときも深さ 1 以上の
    # 行は崩れとして数える ── 単位が無いことを「どの単位でも揃っている」と読むと、
    # まったく字下げしないファイルが満点になる。
    expected_unit = unit or " " * SPACE_UNITS[0]
    broken = tuple(line for line in lines if line.indent != expected_unit * line.level)
    return IndentReport(counted=lines, broken=broken, unit=unit)


def judge_level(report: IndentReport, criterion: RubricCriterion, partial_max_ratio: float) -> int:
    """崩れの割合を観点の段階に割り当てる。"""
    levels = sorted(level.level for level in criterion.levels)
    if not report.broken:
        return levels[-1]
    if len(levels) >= 3 and report.ratio <= partial_max_ratio:
        return levels[-2]
    return levels[0]


class CIndentCheck:
    """C のソースの字下げを入れ子の深さと突き合わせる（決定的）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        targets = [c for c in request.task_version.criteria if c.evaluator_id == EVALUATOR_ID]
        if not targets:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion names c_indent_check"},
            )
        sources = self._sources(request)
        if not sources:
            # ソースが無い。**0 点にしない** ── 出し忘れか読めなかったのかは
            # ここでは分からない。AND なら正しさの観点が先に 0 で止めている。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no C source to check"},
            )
        try:
            partial_max_ratio = float(
                request.options.get("partial_max_ratio", DEFAULT_PARTIAL_MAX_RATIO)  # type: ignore[arg-type]
            )
        except (TypeError, ValueError):
            partial_max_ratio = DEFAULT_PARTIAL_MAX_RATIO

        reports = [(artifact_id, check_indentation(text)) for artifact_id, text in sources]
        counted = sum(len(report.counted) for _, report in reports)
        broken = sum(len(report.broken) for _, report in reports)
        combined = IndentReport(
            counted=tuple(line for _, report in reports for line in report.counted),
            broken=tuple(line for _, report in reports for line in report.broken),
            unit=reports[0][1].unit,
        )
        scores = []
        for criterion in targets:
            level = judge_level(combined, criterion, partial_max_ratio)
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
                    # **確定させる。** 数えた結果に迷いは無い（P3）。
                    conclusive=True,
                    evidence=self._evidence(request, reports),
                    rationale=self._rationale(reports, partial_max_ratio),
                )
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=tuple(scores),
            raw_output={
                "counted_lines": counted,
                "broken_lines": broken,
                "units": [report.unit_label for _, report in reports],
                "partial_max_ratio": partial_max_ratio,
            },
        )

    # -- internals ---------------------------------------------------------

    def _sources(self, request: EvaluationRequest) -> list[tuple[ArtifactId, str]]:
        """判定する C のソース。複数あれば全部（行数を合算する）。"""
        found: list[tuple[ArtifactId, str]] = []
        for artifact in request.submission.gradable_artifacts:
            if artifact.kind.value != "code":
                continue
            if artifact.filename and not artifact.filename.lower().endswith(C_SUFFIXES):
                continue
            content = request.artifact_contents.get(artifact.id)
            if not content:
                continue
            try:
                found.append((artifact.id, content.decode("utf-8")))
            except UnicodeDecodeError:
                continue
        return found

    def _evidence(
        self, request: EvaluationRequest, reports: list[tuple[ArtifactId, IndentReport]]
    ) -> tuple[Evidence, ...]:
        """崩れた行を根拠にする（P4）。崩れが無ければ、数えた範囲を言う。"""
        content_hash = {a.id: a.content_hash for a in request.submission.artifacts}
        evidence = []
        for artifact_id, report in reports:
            shown = report.broken[:MAX_REPORTED_LINES]
            quote = "\n".join(f"{line.number}: {line.text}" for line in shown)
            evidence.append(
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash.get(artifact_id, "unknown"),
                    span=WholeSpan(),
                    quote=quote[:500] or None,
                    note=f"字下げの単位 {report.unit_label}・崩れ {len(report.broken)} 行",
                )
            )
        return tuple(evidence)

    def _rationale(
        self, reports: list[tuple[ArtifactId, IndentReport]], partial_max_ratio: float
    ) -> str:
        parts = []
        for _, report in reports:
            head = (
                f"字下げの単位を{report.unit_label}と推定し、{len(report.counted)} 行を数えました"
            )
            if not report.broken:
                parts.append(f"{head}。すべて入れ子の深さどおりです")
                continue
            numbers = "・".join(str(line.number) for line in report.broken[:MAX_REPORTED_LINES])
            more = " ほか" if len(report.broken) > MAX_REPORTED_LINES else ""
            parts.append(
                f"{head}。{len(report.broken)} 行（{report.ratio:.0%}）が入れ子の深さと"
                f"合っていません（{numbers} 行目{more}）"
            )
        return "。".join(parts) + "。"


def build() -> CIndentCheck:
    """entry point から呼ばれるファクトリ。"""
    return CIndentCheck()


__all__ = [
    "DEFAULT_PARTIAL_MAX_RATIO",
    "EVALUATOR_ID",
    "CIndentCheck",
    "IndentLine",
    "IndentReport",
    "build",
    "check_indentation",
    "code_part",
    "judge_level",
]
