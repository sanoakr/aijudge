"""Sharif Judge の課題ディレクトリを TaskVersion に取り込む。

実際の運用データ（prog2-2025）の形式:

    exNN/
      pN/
        desc.md          問題文。1 行目が `## [必須] タイトル ##`
        desc.html        desc.md から生成した配布用
        in/input1.txt    テストケース入力
        out/output1.txt  期待出力
        <name>.c         参照解答
        <name>           コンパイル済み（取り込まない）

Sharif Judge のコードは継承しないが、既存の課題資産はそのまま使えるようにする。
教員が課題を作り直さずに新システムを試せることが、PoC-0 で実運用に載せる条件。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from aijudge_core import (
    Provenance,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
    derived_id,
)
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId

# `## [必須] 最大値・最小値・平均値 ##` から見出しを取る。
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(?P<title>.+?)\s*#*\s*$")
# 見出し先頭の `[必須]` `[任意]` などの区分。
_TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*")
_INPUT_RE = re.compile(r"^input(?P<index>\d+)\.txt$")

DEFAULT_EVALUATOR = "code_test_runner"
AI_EVALUATOR = "rubric_ai_judge"


class ImportError_(Exception):
    """取り込みに失敗した。データ側の問題なので、内容を添えて上位に返す。"""


def parse_title(desc: str) -> tuple[str, str | None]:
    """desc.md の先頭見出しからタイトルと区分を取り出す。"""
    for line in desc.splitlines():
        if not line.strip():
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            break
        heading = match.group("title")
        tag_match = _TAG_RE.match(heading)
        if tag_match:
            return heading[tag_match.end() :].strip(), tag_match.group("tag")
        return heading.strip(), None
    raise ImportError_("desc.md does not start with a markdown heading")


def collect_test_cases(
    problem_dir: Path, evaluator_id: str = DEFAULT_EVALUATOR
) -> tuple[TestCase, ...]:
    """in/inputN.txt と out/outputN.txt を対にして TestCase にする。

    **テストケースが 0 件でも例外にしない。** 自動採点しない課題が実在する
    （ネットワーク演習の HTTP サーバ課題は in/out が空、レポート課題には
    そもそも無い）。そういう課題は教員レビューだけで運用する。

    ただし「0 件でよい」の判断はここではしない。呼び出し側が
    「自動採点できない課題を黙って作らない」を判断する
    （`aijudge_admin.import_tasks` の `require_test_cases`）。ここで
    例外にすると、その判断を呼び出し側から奪うことになる。

    対応が壊れている場合（input はあるのに output が無い）は例外にする。
    これは「テストが無い」ではなく「テストが壊れている」で、黙って
    件数が減ると採点が緩くなる。
    """
    in_dir = problem_dir / "in"
    out_dir = problem_dir / "out"
    if not in_dir.is_dir() or not out_dir.is_dir():
        return ()

    cases: list[TestCase] = []
    for input_path in sorted(in_dir.iterdir()):
        match = _INPUT_RE.match(input_path.name)
        if match is None:
            continue
        index = match.group("index")
        output_path = out_dir / f"output{index}.txt"
        if not output_path.is_file():
            raise ImportError_(f"{input_path.name} has no matching output{index}.txt")
        cases.append(
            TestCase(
                name=f"case{index}",
                evaluator_id=evaluator_id,
                payload={
                    "input": input_path.read_text(encoding="utf-8"),
                    "expected": output_path.read_text(encoding="utf-8"),
                },
                # Sharif Judge の既定に合わせ、テストの中身は学生に見せない。
                hidden=True,
                weight=1.0,
            )
        )
    return tuple(cases)


def find_reference_solution(problem_dir: Path) -> str | None:
    """参照解答（.c）を読む。無くても取り込みは成立する。"""
    sources = sorted(problem_dir.glob("*.c"))
    if not sources:
        return None
    if len(sources) > 1:
        raise ImportError_(f"{problem_dir} has multiple .c files: {[p.name for p in sources]}")
    return sources[0].read_text(encoding="utf-8")


def _criterion_id(task_key: str, code: str) -> CriterionId:
    """観点 ID を課題と観点コードから決定的に導く。

    同じ課題を取り込み直しても同じ ID になるので、保存済みの採点結果を
    あとから読んでも、どの観点の点なのかが分かる。
    """
    return CriterionId(derived_id("crt", task_key, code))


def correctness_criterion(
    evaluator_id: str = DEFAULT_EVALUATOR, task_key: str = "default"
) -> RubricCriterion:
    """テスト実行だけの課題に与える単一観点。

    段階を 0 / 1 の二値にせず 4 段階にしてあるのは、部分点を表現するため。
    AI 評価器を足すときは、この観点の weight を下げて
    「設計」「可読性」の観点を並べることになる。
    """
    return RubricCriterion(
        id=_criterion_id(task_key, "correctness"),
        code="correctness",
        title="出力の正しさ",
        description="与えられた入力に対して仕様どおりの出力を返すか。テスト実行で判定する。",
        weight=1.0,
        levels=(
            RubricLevel(
                level=0, label="未達", descriptor="ほとんど正しく動作しない", score_ratio=0.0
            ),
            RubricLevel(level=1, label="一部", descriptor="一部のケースで正しい", score_ratio=0.34),
            RubricLevel(level=2, label="概ね", descriptor="大半のケースで正しい", score_ratio=0.67),
            RubricLevel(
                level=3, label="達成", descriptor="すべてのケースで正しい", score_ratio=1.0
            ),
        ),
        evaluator_id=evaluator_id,
    )


def readability_criterion(
    weight: float, evaluator_id: str = AI_EVALUATOR, task_key: str = "default"
) -> RubricCriterion:
    """テスト実行では測れない観点。AI 評価器が担当する。

    Sharif Judge にはこの軸が無かった。テストが通るかどうかしか見ないので、
    「動くが読めない」コードを満点にしてしまう。AI 採点を入れる価値は
    まずここにある。
    """
    return RubricCriterion(
        id=_criterion_id(task_key, "readability"),
        code="readability",
        title="変数名と構造の分かりやすさ",
        description=(
            "変数名が役割を表しているか、処理の流れが追えるか、"
            "不要な重複がないか。出力の正しさはこの観点では評価しない。"
        ),
        weight=weight,
        levels=(
            RubricLevel(
                level=0, label="未達", descriptor="名前が無意味で構造も追えない", score_ratio=0.0
            ),
            RubricLevel(level=1, label="一部", descriptor="一部の名前が説明的", score_ratio=0.34),
            RubricLevel(
                level=2,
                label="概ね",
                descriptor="おおむね説明的で構造も追える",
                score_ratio=0.67,
            ),
            RubricLevel(level=3, label="達成", descriptor="名前・構造とも明快", score_ratio=1.0),
        ),
        evaluator_id=evaluator_id,
    )


def import_problem(
    problem_dir: Path,
    *,
    subject_profile: str,
    authored_by: UserId,
    task_id: TaskId | None = None,
    evaluator_id: str = DEFAULT_EVALUATOR,
    max_score: float = 100.0,
    readability_weight: float = 0.0,
) -> TaskVersion:
    """Sharif Judge の問題ディレクトリ 1 つを TaskVersion にする。

    `readability_weight` を 0 より大きくすると、AI 評価器が担当する
    「読みやすさ」の観点を加え、正しさの重みをその分下げる。
    既存課題をそのまま取り込むだけなら 0 のままでよい。
    """
    if not 0.0 <= readability_weight < 1.0:
        raise ImportError_("readability_weight must be in [0.0, 1.0)")
    if not problem_dir.is_dir():
        raise ImportError_(f"{problem_dir} is not a directory")

    desc_path = problem_dir / "desc.md"
    if not desc_path.is_file():
        raise ImportError_(f"{problem_dir} has no desc.md")

    statement = desc_path.read_text(encoding="utf-8")
    parse_title(statement)  # 形式が壊れていればここで落とす

    # 課題ディレクトリ名を同一性の鍵にする（exNN/pN など運用上の一意名）。
    task_key = f"{problem_dir.parent.name}/{problem_dir.name}"

    correctness = correctness_criterion(evaluator_id, task_key)
    criteria: tuple[RubricCriterion, ...]
    if readability_weight > 0.0:
        criteria = (
            correctness.model_copy(update={"weight": 1.0 - readability_weight}),
            readability_criterion(readability_weight, AI_EVALUATOR, task_key),
        )
    else:
        criteria = (correctness,)

    return TaskVersion(
        id=TaskVersionId(derived_id("tsv", task_key, "1")),
        task_id=task_id or TaskId(derived_id("tsk", task_key)),
        version=1,
        subject_profile=subject_profile,
        statement=statement,
        reference_solution=find_reference_solution(problem_dir),
        criteria=criteria,
        test_cases=collect_test_cases(problem_dir, evaluator_id),
        q_matrix=(),
        max_score=max_score,
        allow_handwriting=False,
        provenance=Provenance(
            authored_by=authored_by,
            # 既存運用で使われてきた課題なので、取り込み時点で承認済みとする。
            review_state=ReviewState.APPROVED,
            reviewed_by=authored_by,
        ),
        created_at=datetime.now(UTC),
    )


def import_assignment(
    assignment_dir: Path,
    *,
    subject_profile: str,
    authored_by: UserId,
    evaluator_id: str = DEFAULT_EVALUATOR,
    readability_weight: float = 0.0,
) -> dict[str, TaskVersion]:
    """exNN/ 配下の p1, p2, … をまとめて取り込む。"""
    problems: dict[str, TaskVersion] = {}
    for problem_dir in sorted(assignment_dir.glob("p[0-9]*")):
        if not problem_dir.is_dir():
            continue
        problems[problem_dir.name] = import_problem(
            problem_dir,
            subject_profile=subject_profile,
            authored_by=authored_by,
            evaluator_id=evaluator_id,
            readability_weight=readability_weight,
        )
    if not problems:
        raise ImportError_(f"{assignment_dir} contains no pN/ problem directories")
    return problems
