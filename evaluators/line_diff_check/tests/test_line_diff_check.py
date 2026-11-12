"""問題のプログラムから変えた行の数の判定が守る約束を固定する（2026-11-13）。

固定したいのは 5 つ。

空白・改行は数えない  字下げ・演算子の前後・空行・行の分け方。定数の中の空白は数える。
変えた行を数える      書き換え・追加・削除。書き換えは多いほうの行数。コメントも行の一部。
上限で段階が決まる    上限以内なら最上段、超えれば最下段。決定的で確定。根拠は変えた行。
比べる相手            言語名の付いたコードブロック（入出力の例ではない）。`block` で指定もできる。
設定の誤りは失敗      上限の宣言や問題文のプログラムが無ければ FAILED。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from textwrap import dedent

from aijudge_eval_line_diff_check import (
    EVALUATOR_ID,
    LineDiffCheck,
    count_changes,
    find_program,
)

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    EvaluatorStatus,
    LineSpan,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import ArtifactId, CriterionId, SubmissionId, TaskId, TaskVersionId, UserId
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def _src(text: str) -> str:
    return dedent(text).lstrip("\n")


# 誤りが 4 か所ある問題のプログラム（exam08 p5 と同じ形）。
PROBLEM = _src(
    """
    #include <stdio.h>

    int main() {
        int n, x, i, k;
        int count[7] = {0};   // count[1]〜count[6]
        scanf("%d", &n);
        if (n < 1 && n > 100) {
            return 0;
        }
        for (i = 0; i <= n; i++) {
            scanf("%d", &x);
            count[x]++;
        }
        for (k = 1; k < 6; k++) {
            printf("%d: %d\\n", k, count[k]);
        }
        int mode = 1;
        for (k = 2; k <= 6; k++) {
            if (count[k] >= count[mode]) {
                mode = k;
            }
        }
        printf("mode %d\\n", mode);
        return 0;
    }
    """
)

FIXED = (
    PROBLEM.replace("if (n < 1 && n > 100) {", "if (n < 1 || n > 100) {  // 修正")
    .replace("i <= n; i++) {", "i < n; i++) {  // 修正")
    .replace("k < 6; k++) {", "k <= 6; k++) {  // 修正")
    .replace("if (count[k] >= count[mode]) {", "if (count[k] > count[mode]) {  // 修正")
)

STATEMENT = f"""## 誤りを直す

次のプログラムには誤りが 4 か所あります。

```c
{PROBLEM}```

#### 入出力の例
入力:
```
3
1 2 2
```
"""


def _changed(source: str) -> int:
    return count_changes(PROBLEM, source).changed


# --------------------------------------------------------------------------
# 空白・改行は数えない
# --------------------------------------------------------------------------


def test_the_problem_itself_changes_nothing() -> None:
    assert _changed(PROBLEM) == 0


def test_whitespace_and_blank_lines_are_not_changes() -> None:
    reformatted = (
        PROBLEM.replace("    ", "\t")
        .replace("int n, x, i, k;", "int n,x,i,k;")
        .replace("count[x]++;", "count[x] ++ ;")
        .replace("return 0;\n}", "\n\nreturn 0;\n}")
    )
    assert _changed(reformatted) == 0


def test_splitting_or_joining_lines_is_not_a_change() -> None:
    split = PROBLEM.replace("for (k = 2; k <= 6; k++) {", "for (k = 2; k <= 6; k++)\n    {")
    assert _changed(split) == 0
    joined = PROBLEM.replace("        mode = k;\n        }", "        mode = k; }")
    assert _changed(joined) == 0


def test_spaces_inside_a_string_are_changes() -> None:
    assert _changed(PROBLEM.replace('"mode %d\\n"', '"mode  %d\\n"')) == 1


# --------------------------------------------------------------------------
# 変えた行を数える
# --------------------------------------------------------------------------


def test_four_fixed_lines_count_four() -> None:
    diff = count_changes(PROBLEM, FIXED)
    assert diff.changed == 4
    assert [line.number for line in diff.submitted] == [7, 10, 14, 19]


def test_a_comment_added_to_an_untouched_line_counts() -> None:
    assert _changed(PROBLEM.replace("count[x]++;", "count[x]++;  // 数える")) == 1


def test_added_and_removed_lines_count() -> None:
    assert (
        _changed(PROBLEM.replace("    int mode = 1;\n", "    int mode = 1;\n    int t = 0;\n")) == 1
    )
    assert _changed(PROBLEM.replace('    printf("mode %d\\n", mode);\n', "")) == 1


def test_a_rewrite_counts_the_larger_side() -> None:
    """2 行を 4 行に書き直せば 4 行（多いほう）。そのまま残った `}` は数えない。"""
    old = "        if (count[k] >= count[mode]) {\n            mode = k;\n        }\n"
    new = (
        "        int better = count[k] > count[mode];\n"
        "        if (better)\n"
        "        {\n"
        "            mode = k;  // 書き直し\n"
        "        }\n"
    )
    assert PROBLEM.count(old) == 1
    assert _changed(PROBLEM.replace(old, new)) == 4


# --------------------------------------------------------------------------
# 比べる相手
# --------------------------------------------------------------------------


def test_the_program_is_the_block_with_a_language() -> None:
    assert find_program(STATEMENT) == PROBLEM


def test_a_block_can_be_chosen_by_number() -> None:
    assert find_program(STATEMENT, 2) == "3\n1 2 2\n"
    assert find_program(STATEMENT, 3) is None


def test_without_a_language_the_first_block_is_used() -> None:
    assert find_program("```\nint main() {}\n```\n") == "int main() {}\n"


# --------------------------------------------------------------------------
# 評価器として
# --------------------------------------------------------------------------


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="fix_scope",
        title="直した範囲",
        description="問題のプログラムから変えた行が指定の行数以内か。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="超えている", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="以内", score_ratio=1.0),
        ),
        evaluator_id=EVALUATOR_ID,
    )


def _limit(value: object = 4, **payload: object) -> TestCase:
    return TestCase(
        name="直した行の数",
        evaluator_id=EVALUATOR_ID,
        payload={"max_changed_lines": value, **payload},
    )


def _request(
    source: str | None,
    *,
    cases: tuple[TestCase, ...] = (),
    statement: str = STATEMENT,
    criterion: RubricCriterion | None = None,
) -> EvaluationRequest:
    artifact_id = ArtifactId("art_" + "3" * 32)
    payload = (source or "").encode("utf-8")
    artifact = Artifact(
        id=artifact_id,
        submission_id=SubmissionId("sub_" + "4" * 32),
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        storage_key="k",
        content_hash="sha256:0",
        byte_size=len(payload),
        filename="main.c",
        created_at=NOW,
    )
    version = TaskVersion(
        id=TaskVersionId("tsv_" + "2" * 32),
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement=statement,
        criteria=(criterion or _criterion(),),
        test_cases=cases,
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=SubmissionId("sub_" + "4" * 32),
        task_version_id=version.id,
        learner_id=UserId("usr_" + "7" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        submitted_at=NOW,
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=version,
        submission=submission,
        artifact_contents={artifact_id: payload} if source is not None else {},
        test_cases=cases,
    )


def test_within_the_limit_gets_the_top_level_conclusively() -> None:
    outcome = LineDiffCheck().evaluate(_request(FIXED, cases=(_limit(4),)))
    assert outcome.status is EvaluatorStatus.OK
    (score,) = outcome.scores
    assert (score.level, score.score_ratio) == (1, 1.0)
    assert score.conclusive and score.confidence == 1.0
    assert score.kind is EvaluatorKind.DETERMINISTIC
    assert "4 行を変えています" in score.rationale and "上限以内" in score.rationale
    assert score.evidence[0].span == LineSpan(start_line=7, end_line=7)


def test_over_the_limit_gets_the_bottom_level() -> None:
    more = FIXED.replace("count[x]++;", "count[x] += 1;")
    outcome = LineDiffCheck().evaluate(_request(more, cases=(_limit(4),)))
    (score,) = outcome.scores
    assert (score.level, score.score_ratio) == (0, 0.0)
    assert "5 行を変えています" in score.rationale and "超えています" in score.rationale


def test_the_limit_is_inclusive() -> None:
    assert LineDiffCheck().evaluate(_request(FIXED, cases=(_limit(4),))).scores[0].level == 1
    assert LineDiffCheck().evaluate(_request(FIXED, cases=(_limit(3),))).scores[0].level == 0


def test_a_missing_or_broken_limit_fails_instead_of_scoring() -> None:
    missing = LineDiffCheck().evaluate(_request(FIXED))
    assert missing.status is EvaluatorStatus.FAILED and missing.scores == ()
    assert missing.error is not None and "max_changed_lines" in missing.error
    broken = LineDiffCheck().evaluate(_request(FIXED, cases=(_limit("4"),)))
    assert broken.status is EvaluatorStatus.FAILED


def test_a_statement_without_a_program_fails() -> None:
    outcome = LineDiffCheck().evaluate(
        _request(FIXED, cases=(_limit(4),), statement="直してください。")
    )
    assert outcome.status is EvaluatorStatus.FAILED


def test_no_source_is_skipped_not_zero() -> None:
    outcome = LineDiffCheck().evaluate(_request(None, cases=(_limit(4),)))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


def test_a_task_without_the_criterion_is_skipped() -> None:
    other = _criterion().model_copy(update={"evaluator_id": "code_test_runner"})
    outcome = LineDiffCheck().evaluate(_request(FIXED, cases=(_limit(4),), criterion=other))
    assert outcome.status is EvaluatorStatus.SKIPPED


def test_the_evaluator_is_registered_as_an_entry_point() -> None:
    names = {ep.name for ep in entry_points(group="aijudge.evaluators")}
    assert EVALUATOR_ID in names
