"""C のソースが**配列の変数を宣言しているか**を決定的に判定する評価器。

## なぜ AI に訊かないか

「`[ ]` を使って宣言した変数があるか」は、ソースを読めば決まる（P3）。2026-11-12 の
prog2 exam08 p6（最大と 2 番目を配列を使わずに求める）では、この観点を
`rubric_ai_judge` に任せていた。読めば分かることを多数決にかけていた。

## 何を「配列を使っている」とみなすか

- **`[ ]` を使って宣言した変数が 1 つでもあれば使っている。** 大きさが変数の
  もの（`int a[n];`）、大きさを省いたもの（`char s[] = "...";`）、多次元、
  `static` などの付いたもの、`for (int a[3] ...` の初期化部の宣言も数える。
- **数えないもの**: 文字列・文字の定数とコメントの中の `[ ]`、添字で読むだけの
  式（`x = a[0];`）、初期化子の中の `[ ]`、関数の引数（`char *argv[]`）。
  引数は値を覚えておく場所ではなく、`main` の引数の書き方で減点しないため。
  プリプロセッサの行（`#define N 100` など）も見ない。
- **`malloc` などで確保して添字で使うものは数えない。** 判定の宣言のとおり
  「`[ ]` を使った宣言」を見る。

宣言かどうかは、文（`;`・`{`・`}` で区切ったもの）が型の語（`int`・`char`・
`double`・`struct X` など）で始まるかで決める。授業の範囲の C ではこれで足りる。
`typedef` で付けた型名から始まる宣言は見分けられない（その場合は配列が無いと
読む ── 誤って 0 点にするより、見逃す側に倒す）。

## 段階

配列の宣言が無ければ最上段、あれば最下段。`conclusive` を立てる（P3）。根拠には
配列を宣言した行を書く。
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

EVALUATOR_ID = "c_array_check"

#: 評価する拡張子。ファイル名が無い提出物（エディタ）は種類だけで決める。
C_SUFFIXES = (".c", ".h")

#: 根拠と説明に書き出す宣言の数の上限。
MAX_REPORTED = 5

#: 宣言の頭（記憶域・修飾子・型）。この並びで始まる文を宣言とみなす。
_TYPE_WORDS = (
    r"typedef|static|const|volatile|register|extern|auto|inline|"
    r"unsigned|signed|short|long|int|char|float|double|void|_Bool|bool|"
    r"size_t|int8_t|int16_t|int32_t|int64_t|uint8_t|uint16_t|uint32_t|uint64_t|"
    r"FILE|"
    r"(?:struct|union|enum)\s+\w+"
)
_DECLARATION = re.compile(rf"^\s*(?:for\s*\(\s*)?((?:(?:{_TYPE_WORDS})\b[\s*]*)+)(.*)$", re.DOTALL)
#: 構造体・共用体・列挙の本体の頭（`struct point {`・`typedef struct {`）。
_AGGREGATE = re.compile(r"^\s*(?:typedef\s+)?(?:static\s+|const\s+)*(?:struct|union|enum)\b")
#: 本体を閉じた直後の宣言子の並び（型の語は本体の前にある）。
_DECLARATORS = re.compile(r"^(\s*[*\s]*)(.*)$", re.DOTALL)


def blank_out(source: str) -> str:
    """コメント・文字列・文字の定数・プリプロセッサの行を空白に置き換える。

    **位置は保つ**（改行は残す）── 行番号を根拠に書くため。
    """
    out = list(source)
    i, n = 0, len(source)
    line_start = True
    while i < n:
        ch = source[i]
        if line_start and ch in " \t":
            i += 1
            continue
        if line_start and ch == "#":
            # プリプロセッサの行（行末の `\` で続く分も）。
            while i < n and source[i] != "\n":
                if source[i] == "\\" and i + 1 < n and source[i + 1] == "\n":
                    out[i] = " "
                    i += 2
                    continue
                out[i] = " "
                i += 1
            continue
        line_start = False
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for k in range(i, end):
                if source[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if ch in "\"'":
            # 定数の中身は消し、引用符は残す（`char s[] = "..."` の形が崩れない）。
            j = i + 1
            while j < n and source[j] != ch and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
            continue
        if ch == "\n":
            line_start = True
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class ArrayDeclaration:
    """配列の宣言 1 つ。行は 1 始まり。"""

    line: int
    text: str


def _split_top(text: str, sep: str) -> list[tuple[int, str]]:
    """括弧の外の `sep` で切る。各片の先頭の位置（`text` の中）も返す。"""
    parts: list[tuple[int, str]] = []
    depth = 0
    start = 0
    for k, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append((start, text[start:k]))
            start = k + 1
    parts.append((start, text[start:]))
    return parts


def _array_bracket(declarator: str) -> int | None:
    """宣言子が配列なら、その `[` の位置。初期化子と関数の引数は見ない。"""
    head = _split_top(declarator, "=")[0][1]
    # 関数の宣言子（`main(int argc, char *argv[])`）は引数の並びを捨てる。
    paren = re.search(r"[A-Za-z_]\w*\s*\(", head)
    if paren is not None:
        head = head[: paren.end() - 1]
    position = head.find("[")
    return position if position >= 0 else None


def find_arrays(source: str) -> tuple[ArrayDeclaration, ...]:
    """配列の変数の宣言を、出てくる順に返す。"""
    cleaned = blank_out(source)
    lines = source.split("\n")
    found: list[ArrayDeclaration] = []
    start = 0
    # 開いている `{` が構造体などの本体か。本体を閉じた直後の片は宣言子の並び
    # （`struct point { ... } pts[3];` の `pts[3]`）なので、型の語が無くても宣言として読む。
    bodies: list[bool] = []
    after_body = False
    for k, ch in enumerate(cleaned + ";"):
        if ch not in ";{}":
            continue
        segment = cleaned[start:k]
        offset = start
        start = k + 1
        declarators_only = after_body
        after_body = False
        if ch == "{":
            bodies.append(_AGGREGATE.match(segment) is not None)
        elif ch == "}":
            after_body = bool(bodies) and bodies.pop()
        match = (_DECLARATORS if declarators_only else _DECLARATION).match(segment)
        if match is None:
            continue
        rest_at = offset + match.start(2)
        for part_at, declarator in _split_top(match.group(2), ","):
            bracket = _array_bracket(declarator)
            if bracket is None:
                continue
            at = rest_at + part_at + bracket
            line = cleaned.count("\n", 0, at) + 1
            found.append(ArrayDeclaration(line=line, text=lines[line - 1].strip()))
    return tuple(found)


def judge_level(arrays: tuple[ArrayDeclaration, ...], criterion: RubricCriterion) -> int:
    levels = sorted(level.level for level in criterion.levels)
    return levels[0] if arrays else levels[-1]


class CArrayCheck:
    """C のソースが配列の変数を宣言しているかを見る（決定的）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        targets = [c for c in request.task_version.criteria if c.evaluator_id == EVALUATOR_ID]
        if not targets:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion names c_array_check"},
            )
        found = self._source(request)
        if found is None:
            # ソースが無い・読めない。**0 点にしない**（`c_indent_check` と同じ判断）。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED, raw_output={"reason": "no C source to check"}
            )
        artifact_id, content_hash, source = found
        arrays = find_arrays(source)
        evidence = self._evidence(artifact_id, content_hash, arrays)
        rationale = self._rationale(arrays)
        scores = []
        for criterion in targets:
            level = judge_level(arrays, criterion)
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
                    # **確定させる。** 宣言の有無に迷いは無い（P3）。
                    conclusive=True,
                    evidence=evidence,
                    rationale=rationale,
                )
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=tuple(scores),
            raw_output={"arrays": [{"line": a.line, "text": a.text} for a in arrays]},
        )

    # -- internals ---------------------------------------------------------

    def _source(self, request: EvaluationRequest) -> tuple[ArtifactId, str, str] | None:
        """判定する C のソース。コードの提出物の最初の 1 つ（エディタの提出は 1 ファイル）。"""
        for artifact in request.submission.gradable_artifacts:
            if artifact.kind.value != "code":
                continue
            if artifact.filename and not artifact.filename.lower().endswith(C_SUFFIXES):
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
        self, artifact_id: ArtifactId, content_hash: str, arrays: tuple[ArrayDeclaration, ...]
    ) -> tuple[Evidence, ...]:
        if not arrays:
            return (
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=WholeSpan(),
                    note="配列の宣言なし",
                ),
            )
        return tuple(
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=LineSpan(start_line=a.line, end_line=a.line),
                quote=f"{a.line}: {a.text}"[:500],
                note="配列の宣言",
            )
            for a in arrays[:MAX_REPORTED]
        )

    def _rationale(self, arrays: tuple[ArrayDeclaration, ...]) -> str:
        if not arrays:
            return "配列（[ ] を使った宣言）は使っていません。"
        shown = "、".join(f"{a.line} 行目「{a.text}」" for a in arrays[:MAX_REPORTED])
        more = f" ほか {len(arrays) - MAX_REPORTED} か所" if len(arrays) > MAX_REPORTED else ""
        return f"配列を宣言しています: {shown}{more}。"


def build() -> CArrayCheck:
    """entry point から呼ばれるファクトリ。"""
    return CArrayCheck()


__all__ = [
    "EVALUATOR_ID",
    "ArrayDeclaration",
    "CArrayCheck",
    "blank_out",
    "build",
    "find_arrays",
    "judge_level",
]
