"""提出が、問題文のプログラムから**何行を変えたか**を数え、上限以内かを決定的に判定する評価器。

## なぜ AI に訊かないか

「誤りを直す」問題で、直した範囲が誤りの行に収まっているか（プログラム全体を書き
直していないか）は、問題のプログラムと提出を比べれば決まる（P3）。2026-11-12 の
prog2 exam08 p5（誤り 4 か所）では、この観点を `rubric_ai_judge` に任せていた。

## 比べるもの

- **問題のプログラム**: 問題文（`TaskVersion.statement`）のコードブロック。言語名の
  付いた最初のブロック（無ければ最初のブロック）。入出力の例のブロックを取らない
  ため。`block` で何番目かを指定できる（1 始まり）。
- **上限**: 課題の `test_cases` のうち、この評価器あての宣言の `max_changed_lines`。
  定義ファイルでは `extra_test_cases` に書く（問題ディレクトリの入出力のテストを
  残したまま足せる）。

    extra_test_cases:
      - {name: 直した行の数, evaluator: line_diff_check, payload: {max_changed_lines: 4}}

## 数え方

- **文字列・文字の定数の外の空白・タブは数えない。** 字下げや演算子の前後の空白を
  変えただけの行は、変えた行に数えない（定数の中の空白は出力を変えるので数える）。
- **改行の違いは数えない。** 空行を足した・消しただけでは数えない。1 行を 2 行に
  分けた・2 行を 1 行にまとめただけ（空白を除いて中身が同じ）も数えない。
- それ以外は、変えた行・足した行・消した行を数える。1 行を書き換えたら 1 行、
  2 行を 4 行に書き換えたら 4 行（多いほう）。そのまま残った行（`}` など）は数えない。
  コメントも行の一部である（行末に `// 修正` を足した行は、変えた行に数える）。

変えた行が上限以内なら最上段、超えていれば最下段。`conclusive` を立てる（P3）。
**直し方が正しいかは見ない** ── それは出力の正しさの観点（`code_test_runner`）が見る。

上限の宣言か、問題文のプログラムが見つからなければ `FAILED` にする。黙って点を
付けると設定の誤りが成績に出るが、失敗なら観点が未採点のままレビューに回る。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    LineSpan,
    RubricCriterion,
    TestCase,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "line_diff_check"

#: 根拠と説明に書き出す行の数の上限。
MAX_REPORTED = 10

#: 問題文のコードブロック（``` で囲む）。1 つ目の群が言語名。
_FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


# --------------------------------------------------------------------------
# 正規化
# --------------------------------------------------------------------------


def normalize_line(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """1 行から、文字列・文字の定数の外にある空白を取り除く。

    コメントの中の `'` や `"` を定数の始まりと読まないよう、`//` と `/* */` を追う。
    コメントの中身は残す（行の一部である）。
    """
    out: list[str] = []
    i, n = 0, len(line)
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
            out.append("".join(c for c in line[i:] if not c.isspace()))
            break
        if line.startswith("/*", i):
            out.append("/*")
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


def find_program(statement: str, block: int | None = None) -> str | None:
    """問題文のプログラム。`block` が無ければ、言語名の付いた最初のブロック。"""
    blocks = [(m.group(1).strip(), m.group(2)) for m in _FENCE.finditer(statement)]
    if not blocks:
        return None
    if block is not None:
        return blocks[block - 1][1] if 1 <= block <= len(blocks) else None
    for language, body in blocks:
        if language:
            return body
    return blocks[0][1]


# --------------------------------------------------------------------------
# 数える
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Diff:
    """変えた行の数と、提出の側の変えた行（根拠に書く）。"""

    changed: int
    submitted: tuple[Line, ...]
    removed: tuple[Line, ...]


def count_changes(original: str, submitted: str) -> Diff:
    """問題のプログラムから、提出で変えた行の数。"""
    before = normalized_lines(original)
    after = normalized_lines(submitted)
    matcher = SequenceMatcher(
        None, [line.text for line in before], [line.text for line in after], autojunk=False
    )
    changed = 0
    touched: list[Line] = []
    removed: list[Line] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        # 改行の位置だけが違う（分けた・まとめた）なら数えない。
        if "".join(line.text for line in old) == "".join(line.text for line in new):
            continue
        changed += max(len(old), len(new))
        touched.extend(new)
        if not new:
            removed.extend(old)
    return Diff(changed=changed, submitted=tuple(touched), removed=tuple(removed))


# --------------------------------------------------------------------------
# 評価器
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Limit:
    max_changed_lines: int
    block: int | None
    criterion: str | None


def _limits(cases: tuple[TestCase, ...]) -> list[Limit]:
    limits = []
    for case in cases:
        if case.evaluator_id != EVALUATOR_ID:
            continue
        payload = case.payload
        value = payload.get("max_changed_lines")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"「{case.name}」の max_changed_lines は 0 以上の整数にしてください")
        block = payload.get("block")
        if block is not None and (not isinstance(block, int) or isinstance(block, bool)):
            raise ValueError(f"「{case.name}」の block は整数にしてください")
        criterion = payload.get("criterion")
        limits.append(
            Limit(
                max_changed_lines=value,
                block=block,
                criterion=str(criterion) if criterion is not None else None,
            )
        )
    return limits


def judge_level(diff: Diff, limit: int, criterion: RubricCriterion) -> int:
    levels = sorted(level.level for level in criterion.levels)
    return levels[-1] if diff.changed <= limit else levels[0]


class LineDiffCheck:
    """問題のプログラムから変えた行の数を、上限と比べる（決定的）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC
    # 課題の宣言（`max_changed_lines`）を読む（#300）。形は画面の知らない名前にする ──
    # 画面は知らない形を編集させず「画面から編集できない」と言う（`test_case_shape`）。
    # 入出力の欄で編集させると、上限の宣言が入出力の組に書き換わって壊れる。
    uses_test_cases = True
    test_case_shape = "limit"

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        targets = [c for c in request.task_version.criteria if c.evaluator_id == EVALUATOR_ID]
        if not targets:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion names line_diff_check"},
            )
        try:
            limits = _limits(request.test_cases or request.task_version.test_cases)
        except ValueError as exc:
            return EvaluationOutcome(status=EvaluatorStatus.FAILED, error=str(exc))
        found = self._source(request)
        if found is None:
            # ソースが無い・読めない。**0 点にしない**（ほかの決定的評価器と同じ判断）。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED, raw_output={"reason": "no source to compare"}
            )
        artifact_id, content_hash, source = found

        scores = []
        details = []
        for criterion in targets:
            mine = [x for x in limits if x.criterion in (None, criterion.code)]
            if not mine:
                return EvaluationOutcome(
                    status=EvaluatorStatus.FAILED,
                    error=f"観点「{criterion.code}」の上限（max_changed_lines）が宣言されていません",
                )
            limit = mine[0]
            program = find_program(request.task_version.statement, limit.block)
            if program is None:
                return EvaluationOutcome(
                    status=EvaluatorStatus.FAILED,
                    error="問題文にプログラム（コードブロック）がありません",
                )
            diff = count_changes(program, source)
            level = judge_level(diff, limit.max_changed_lines, criterion)
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
                    evidence=self._evidence(artifact_id, content_hash, diff),
                    rationale=self._rationale(diff, limit.max_changed_lines),
                )
            )
            details.append(
                {
                    "criterion": criterion.code,
                    "changed": diff.changed,
                    "max_changed_lines": limit.max_changed_lines,
                    "lines": [line.number for line in diff.submitted],
                }
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK, scores=tuple(scores), raw_output={"criteria": details}
        )

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
        self, artifact_id: ArtifactId, content_hash: str, diff: Diff
    ) -> tuple[Evidence, ...]:
        if not diff.submitted:
            return (
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=WholeSpan(),
                    note=f"変えた行 {diff.changed} 行",
                ),
            )
        return tuple(
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=LineSpan(start_line=line.number, end_line=line.number),
                quote=f"{line.number}: {line.raw.strip()}"[:500],
                note="問題のプログラムから変えた行",
            )
            for line in diff.submitted[:MAX_REPORTED]
        )

    def _rationale(self, diff: Diff, limit: int) -> str:
        numbers = "・".join(str(line.number) for line in diff.submitted[:MAX_REPORTED])
        more = " ほか" if len(diff.submitted) > MAX_REPORTED else ""
        where = f"（提出の {numbers} 行目{more}）" if numbers else ""
        removed = f"。消した行が {len(diff.removed)} 行あります" if diff.removed else ""
        verdict = "上限以内です" if diff.changed <= limit else "上限を超えています"
        return (
            f"問題のプログラムから {diff.changed} 行を変えています{where}{removed}。"
            f"上限は {limit} 行で、{verdict}（空白・改行の違いは数えません）。"
        )


def build() -> LineDiffCheck:
    """entry point から呼ばれるファクトリ。"""
    return LineDiffCheck()


__all__ = [
    "EVALUATOR_ID",
    "Diff",
    "LineDiffCheck",
    "build",
    "count_changes",
    "find_program",
    "judge_level",
    "normalize_line",
    "normalized_lines",
]
