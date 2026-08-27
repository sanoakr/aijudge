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

from . import companion

# `## [必須] 最大値・最小値・平均値 ##` から見出しを取る。
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(?P<title>.+?)\s*#*\s*$")
# 見出し先頭の `[必須]` `[任意]` などの区分。
_TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*")
_INPUT_RE = re.compile(r"^input(?P<index>\d+)\.txt$")

DEFAULT_EVALUATOR = "code_test_runner"
AI_EVALUATOR = "rubric_ai_judge"


class ImportError_(Exception):
    """取り込みに失敗した。データ側の問題なので、内容を添えて上位に返す。"""


# `ex06` / `ex07-2023` / `exam08` から回を取る。
_UNIT_SESSION = re.compile(r"^ex(?P<n>\d+)(?:-\d+)?$", re.IGNORECASE)
# `p3` から問の順序を取る。
_POSITION = re.compile(r"^p(?P<n>\d+)$", re.IGNORECASE)


def parse_unit(problem_dir: Path) -> tuple[str, int | None, int | None]:
    """課題ディレクトリから「何回目の何問目か」を取る。

    Sharif Judge の配置は `ex06/p3` のようになっている。1 回の授業で複数問
    出るので、これが分からないと一覧が平らになり、学習者も教員も何回目の
    分を見ているのか分からなくなる。

    `exam08` や `2025-exam16` のように回に対応しない名前もあるので、
    取れなければ `None` を返す（まとまりの名前だけは残る）。
    """
    unit = problem_dir.parent.name
    session_match = _UNIT_SESSION.match(unit)
    session = int(session_match.group("n")) if session_match else None

    position_match = _POSITION.match(problem_dir.name)
    position = int(position_match.group("n")) if position_match else None
    return unit, session, position


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


# 参照解答として扱う拡張子。科目によって言語が違う（prog2 は C、
# ネットワーク演習は Python）ので、決め打ちにしない。
REFERENCE_SUFFIXES = (".c", ".py", ".java")


def find_reference_solution(problem_dir: Path) -> str | None:
    """参照解答を読む。無くても取り込みは成立する。

    複数あっても落とさない。既存の課題ディレクトリには `a.out` や別年度の
    控えが混ざっていることがあり、そこで取り込みを止めると 130 件の課題が
    1 件の残骸で入らなくなる。拡張子の優先順で 1 つ選ぶ。
    """
    for suffix in REFERENCE_SUFFIXES:
        sources = sorted(problem_dir.glob(f"*{suffix}"))
        if sources:
            return sources[0].read_text(encoding="utf-8", errors="replace")
    return None


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

    # 伴走プロセスの宣言があればそちらを使う。クライアント／サーバ課題は
    # in/ out/ の形に乗らない（ADR 0008）。
    if companion.has_companion(problem_dir):
        cases = companion.load_companion_cases(problem_dir)
        graded_by_id = companion.EVALUATOR_ID
    else:
        cases = collect_test_cases(problem_dir, evaluator_id)
        graded_by_id = evaluator_id

    # **テストケースが無い課題は、決定的評価器に担当させない。**
    #
    # 担当させると、その観点は永久に採点されないまま `unscored_criteria` に
    # 入り、全提出が review_required で教員に積まれる（設計原則 P5 の
    # 「誰も見ていない観点に点を与えない」が、そのまま「全部を人間が見る」に
    # なる）。
    #
    # 実在する課題がこれに当たる。ネットワーク演習の HTTP サーバ課題
    # （受動的に応答するので Sharif Judge では判定できなかった）、
    # プログラミング演習の自己採点課題、レポート課題。
    #
    # これらは「自動採点できない課題」ではなく「**まだ**自動採点できない課題」
    # である（伴走サーバによるサーバ課題の採点、レポートのルーブリック採点は
    # いずれも計画にある）。当面は AI 観点だけで構成し、テストケースの形が
    # 決まった時点で新しい版を作って決定的評価器に渡す。
    graded_by = graded_by_id if cases else AI_EVALUATOR
    correctness = correctness_criterion(graded_by, task_key)
    if not cases:
        correctness = correctness.model_copy(
            update={
                "title": "仕様の充足",
                "description": (
                    "課題の指示どおりに動作するか。自動テストがまだ無いため "
                    "AI が判定し、教員が確定させる。"
                ),
            }
        )

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
        test_cases=cases,
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
