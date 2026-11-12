"""テンプレートの範囲の外を変えていないかの判定が守る約束を固定する（2026-11-13）。

固定したいのは 5 つ。

範囲の中は自由      何行書いても、テンプレートの固定部分と同じ行を書いてもよい。目印は消してよい。
空白は数えない      字下げ・空行・演算子の前後の空白。ただし文字列の中の空白は数える。
外は全部そろえる    範囲の外の行（コメントを含む）を変える・消す・足すと「変えている」。
目印の外側も外      目印を残したまま、開始の前・終了の後ろに足した行は範囲の外。
設定の誤りは失敗    問題文にテンプレートが無ければ FAILED（黙って点を付けない）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from textwrap import dedent

from aijudge_eval_template_diff_check import (
    EVALUATOR_ID,
    TemplateDiffCheck,
    check_template,
    find_template,
    normalize_line,
    split_template,
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
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "4" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
START = "// ここから"
END = "// ここまで"


def _src(text: str) -> str:
    return dedent(text).lstrip("\n")


TEMPLATE = _src(
    """
    #include <stdio.h>

    int main() {
        int n, x;
        int count[2] = {0};   // count[0] が偶数、count[1] が奇数の個数

        scanf("%d", &n);
        for (int i = 0; i < n; i++) {
            scanf("%d", &x);
            // ここから

            // ここまで
        }
        printf("%d %d\\n", count[0], count[1]);
        return 0;
    }
    """
)

STATEMENT = f"""## 偶数と奇数を数える

次のプログラムの `// ここから` と `// ここまで` の間を書いてください。

```c
{TEMPLATE}```

#### 入出力の例
```
3
1 2 3
```
"""

# 範囲を埋めた解答。範囲の中に、固定部分と同じ行（`}`）を何度も書いている。
ANSWER = TEMPLATE.replace(
    "        // ここから\n\n        // ここまで\n",
    "        // ここから\n"
    "        if (x % 2 == 0) {\n"
    "            count[0]++;\n"
    "        } else {\n"
    "            count[1]++;\n"
    "        }\n"
    "        // ここまで\n",
)


def _kept(source: str) -> bool:
    return check_template(source, split_template(TEMPLATE, START, END)).kept


# --------------------------------------------------------------------------
# 範囲の中は自由
# --------------------------------------------------------------------------


def test_the_template_itself_is_kept() -> None:
    assert _kept(TEMPLATE)


def test_a_filled_region_is_kept_even_with_closing_braces_inside() -> None:
    """範囲の中の `}` が固定部分の `}` と同じでも、後ろの固定部分を取り違えない。"""
    assert _kept(ANSWER)


def test_the_markers_may_be_deleted() -> None:
    without = ANSWER.replace("        // ここから\n", "").replace("        // ここまで\n", "")
    assert _kept(without)


def test_only_one_marker_may_be_deleted() -> None:
    assert _kept(ANSWER.replace("        // ここから\n", ""))
    assert _kept(ANSWER.replace("        // ここまで\n", ""))


# --------------------------------------------------------------------------
# 空白は数えない
# --------------------------------------------------------------------------


def test_indentation_blank_lines_and_spaces_are_not_changes() -> None:
    reformatted = (
        ANSWER.replace("    ", "\t")
        .replace("int n, x;", "int n,x;")
        .replace("i < n; i++", "i<n;i++")
        .replace("return 0;", "\n\nreturn 0;")
    )
    assert _kept(reformatted)


def test_crlf_line_endings_are_not_changes() -> None:
    assert _kept(ANSWER.replace("\n", "\r\n"))


def test_spaces_inside_a_string_are_changes() -> None:
    """文字列の中の空白は出力を変える。空白の正規化で消してはいけない。"""
    assert not _kept(ANSWER.replace('"%d %d\\n"', '"%d  %d\\n"'))


def test_a_quote_inside_a_comment_does_not_start_a_string() -> None:
    assert normalize_line("x = 1; // it's  ok", False) == ("x=1;//it'sok", False)
    assert normalize_line("/* it's", False) == ("/*it's", True)
    assert normalize_line("still */ y = 2;", True) == ("still*/y=2;", False)


# --------------------------------------------------------------------------
# 外は全部そろえる
# --------------------------------------------------------------------------


def test_changing_a_line_outside_is_caught_and_named() -> None:
    changed = ANSWER.replace("int count[2] = {0};", "int count[3] = {0};")
    verdict = check_template(changed, split_template(TEMPLATE, START, END))
    assert not verdict.kept
    assert verdict.submitted is not None and verdict.submitted.number == 5
    assert verdict.expected is not None and "count[2]" in verdict.expected.raw


def test_deleting_a_line_outside_is_caught() -> None:
    assert not _kept(ANSWER.replace("    return 0;\n", ""))


def test_changing_a_comment_outside_is_caught() -> None:
    assert not _kept(ANSWER.replace("偶数、count[1]", "偶数で count[1]"))


def test_adding_a_line_at_the_end_is_caught() -> None:
    assert not _kept(ANSWER + "// おわり\n")


def test_moving_the_input_into_the_region_is_caught() -> None:
    """範囲の外の `scanf` を消して範囲の中に書き直しても、外を変えたことになる。"""
    moved = ANSWER.replace(
        '        scanf("%d", &x);\n        // ここから\n',
        '        // ここから\n        scanf("%d", &x);\n',
    )
    assert not _kept(moved)


# --------------------------------------------------------------------------
# 目印の外側も外
# --------------------------------------------------------------------------


def test_a_line_added_before_the_start_marker_is_outside() -> None:
    added = ANSWER.replace("        // ここから\n", "        x = x * 2;\n        // ここから\n")
    verdict = check_template(added, split_template(TEMPLATE, START, END))
    assert not verdict.kept
    assert verdict.submitted is not None and "x = x * 2;" in verdict.submitted.raw


def test_a_line_added_after_the_end_marker_is_outside() -> None:
    added = ANSWER.replace("        // ここまで\n", "        // ここまで\n        x = 0;\n")
    verdict = check_template(added, split_template(TEMPLATE, START, END))
    assert not verdict.kept
    assert verdict.submitted is not None and "x = 0;" in verdict.submitted.raw


def test_the_same_line_is_inside_once_the_marker_is_deleted() -> None:
    """目印を消せば、範囲の境目は固定部分で決まる。同じ行が範囲の中になる。"""
    added = ANSWER.replace("        // ここから\n", "        x = x * 2;\n")
    assert _kept(added)


# --------------------------------------------------------------------------
# 範囲が複数あるテンプレート
# --------------------------------------------------------------------------

TWO_REGIONS = _src(
    """
    int a(void) {
        // ここから
        // ここまで
    }
    int b(void) {
        // ここから
        // ここまで
    }
    """
)


def test_two_regions_both_filled_are_kept() -> None:
    template = split_template(TWO_REGIONS, START, END)
    filled = _src(
        """
        int a(void) {
            // ここから
            return 1;
            // ここまで
        }
        int b(void) {
            return 2;
        }
        """
    )
    assert template.regions == 2
    assert check_template(filled, template).kept


def test_changing_the_part_between_two_regions_is_caught() -> None:
    template = split_template(TWO_REGIONS, START, END)
    assert not check_template(TWO_REGIONS.replace("int b(void)", "int c(void)"), template).kept


# --------------------------------------------------------------------------
# テンプレートの取り出し
# --------------------------------------------------------------------------


def test_the_template_is_the_code_block_with_the_start_marker() -> None:
    assert find_template(STATEMENT, START) == TEMPLATE


def test_inline_mentions_of_the_marker_are_not_a_template() -> None:
    assert find_template("`// ここから` と `// ここまで` の間を書く。", START) is None


# --------------------------------------------------------------------------
# 評価器として
# --------------------------------------------------------------------------


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="keep_template",
        title="指定の範囲の外を変えていないか",
        description="範囲の外を問題のプログラムから変えていないか。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="変えている", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="変えていない", score_ratio=1.0),
        ),
        evaluator_id=EVALUATOR_ID,
    )


def _request(
    source: str | None,
    *,
    statement: str = STATEMENT,
    criterion: RubricCriterion | None = None,
    options: dict[str, object] | None = None,
) -> EvaluationRequest:
    artifact_id = ArtifactId("art_" + "3" * 32)
    payload = (source or "").encode("utf-8")
    artifact = Artifact(
        id=artifact_id,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        storage_key="k",
        content_hash="sha256:0",
        byte_size=len(payload),
        filename="main.c",
        created_at=NOW,
    )
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement=statement,
        criteria=(criterion or _criterion(),),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=SUBMISSION,
        task_version_id=VERSION,
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
        options=options or {},
    )


def test_a_kept_template_gets_the_top_level_conclusively() -> None:
    outcome = TemplateDiffCheck().evaluate(_request(ANSWER))
    assert outcome.status is EvaluatorStatus.OK
    (score,) = outcome.scores
    assert (score.level, score.score_ratio) == (1, 1.0)
    assert score.conclusive and score.confidence == 1.0
    assert score.kind is EvaluatorKind.DETERMINISTIC
    assert outcome.raw_output["kept"] is True


def test_a_changed_template_gets_the_bottom_level_with_the_line_as_evidence() -> None:
    changed = ANSWER.replace("int count[2] = {0};", "int count[3] = {0};")
    outcome = TemplateDiffCheck().evaluate(_request(changed))
    (score,) = outcome.scores
    assert (score.level, score.score_ratio) == (0, 0.0)
    assert score.conclusive
    assert "5 行目" in score.rationale and "count[2]" in score.rationale
    (evidence,) = score.evidence
    assert evidence.span == LineSpan(start_line=5, end_line=5)
    assert outcome.raw_output["kept"] is False


def test_a_statement_without_a_template_fails_instead_of_scoring() -> None:
    outcome = TemplateDiffCheck().evaluate(_request(ANSWER, statement="和を出力せよ。"))
    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.scores == ()
    assert outcome.error is not None and START in outcome.error


def test_an_unpaired_marker_in_the_template_fails() -> None:
    broken = STATEMENT.replace("// ここまで\n", "")
    outcome = TemplateDiffCheck().evaluate(_request(ANSWER, statement=broken))
    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.error is not None and "終了の目印" in outcome.error


def test_no_source_is_skipped_not_zero() -> None:
    outcome = TemplateDiffCheck().evaluate(_request(None))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


def test_a_task_without_the_criterion_is_skipped() -> None:
    other = _criterion().model_copy(update={"evaluator_id": "code_test_runner"})
    outcome = TemplateDiffCheck().evaluate(_request(ANSWER, criterion=other))
    assert outcome.status is EvaluatorStatus.SKIPPED


def test_markers_can_be_set_by_the_profile() -> None:
    template = "n = int(input())\n# ここから\n# ここまで\nprint(n)\n"
    statement = f"```python\n{template}```\n"
    source = "n = int(input())\nn = n * 2\nprint(n)\n"
    options: dict[str, object] = {"start_marker": "# ここから", "end_marker": "# ここまで"}
    outcome = TemplateDiffCheck().evaluate(_request(source, statement=statement, options=options))
    assert outcome.raw_output["kept"] is True


def test_the_evaluator_is_registered_as_an_entry_point() -> None:
    names = {ep.name for ep in entry_points(group="aijudge.evaluators")}
    assert EVALUATOR_ID in names
