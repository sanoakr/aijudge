"""C の字下げの判定が守る約束を固定する（2026-10-11）。

固定したいのは 5 つ。

単位は問わない      2・3・4・8 スペースでもタブでも、揃っていれば満点。
同じ深さは同じ幅    同じ深さで 3 文字と 4 文字が混ざれば、推定した単位に合わない側が崩れ。
深さは構文から      `{ }`・波括弧を省いた制御文・switch の 2 つの流儀・括弧を次の行に置く書き方。
数えない行がある    空行・プリプロセッサ・コメント・式の折り返し。
段階は割合で        0 行なら最上段、20% 以下なら中段、超えれば最下段。決定的で確定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from textwrap import dedent

from aijudge_eval_c_indent_check import (
    EVALUATOR_ID,
    CIndentCheck,
    check_indentation,
)

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    EvaluatorStatus,
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

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "4" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)


def _src(text: str) -> str:
    return dedent(text).lstrip("\n")


def _broken(text: str) -> list[int]:
    return [line.number for line in check_indentation(_src(text)).broken]


GOOD_4 = """
#include <stdio.h>

int main(void)
{
    int i, sum = 0;
    for (i = 0; i < 10; i++) {
        if (i % 2 == 0) {
            sum += i;
        }
    }
    printf("%d\\n", sum);
    return 0;
}
"""


# --------------------------------------------------------------------------
# 単位は問わない
# --------------------------------------------------------------------------


def test_four_spaces_consistently_pass() -> None:
    report = check_indentation(_src(GOOD_4))
    assert report.broken == ()
    assert report.unit_label == "4 スペース"


def test_three_spaces_consistently_pass() -> None:
    report = check_indentation(_src(GOOD_4).replace("    ", "   "))
    assert report.broken == ()
    assert report.unit_label == "3 スペース"


def test_tabs_consistently_pass() -> None:
    report = check_indentation(_src(GOOD_4).replace("    ", "\t"))
    assert report.broken == ()
    assert report.unit_label == "タブ"


# --------------------------------------------------------------------------
# 同じ深さは同じ幅
# --------------------------------------------------------------------------


def test_three_and_four_at_the_same_depth_are_not_the_same() -> None:
    """同じ深さで 3 文字と 4 文字が混ざれば、少ない側（推定に合わない側）が崩れ。"""
    assert _broken(
        """
        int main(void)
        {
            int a = 1;
            int b = 2;
           int c = 3;
            return a + b + c;
        }
        """
    ) == [5]


def test_a_line_one_short_inside_a_nested_block_is_caught() -> None:
    """参照解答 ex03/p8 に実際にあった形（12 文字のところが 11 文字）。"""
    assert _broken(
        """
        int main(void)
        {
            while (n > 1) {
                if (n % p == 0) {
                    n /= p;
                } else {
                   if (kp > 0) {
                        kp = 0;
                    }
                }
            }
            return 0;
        }
        """
    ) == [7]


def test_no_indentation_at_all_breaks_every_nested_line() -> None:
    report = check_indentation("int main(void)\n{\nint a = 1;\nreturn a;\n}\n")
    assert [line.number for line in report.broken] == [3, 4]


def test_mixing_tabs_and_spaces_counts_the_minority_as_broken() -> None:
    source = "int main(void)\n{\n\tint a = 1;\n\tint b = 2;\n    return a + b;\n}\n"
    assert [line.number for line in check_indentation(source).broken] == [5]


# --------------------------------------------------------------------------
# 深さは構文から
# --------------------------------------------------------------------------


def test_a_braceless_body_is_one_level_deeper() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                if (a)
                    b = 1;
                else if (c)
                    b = 2;
                else
                    b = 3;
                for (i = 0; i < n; i++)
                    if (i > 2)
                        s += i;
                return 0;
            }
            """
        )
        == []
    )


def test_a_braceless_body_left_at_the_same_depth_is_broken() -> None:
    assert _broken(
        """
        int main(void)
        {
            if (a)
            b = 1;
            return 0;
        }
        """
    ) == [4]


def test_a_one_line_if_does_not_indent_the_next_line() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                if (p == 2) p++;
                else p += 2;
                return 0;
            }
            """
        )
        == []
    )


def test_else_after_a_one_line_if_keeps_the_outer_body_level() -> None:
    """波括弧を省いた `for` の本体の中で、1 行の `if` に `else` が続く形（2026-10-11）。

    1 行で閉じた `if` が外側の `for` の下げまで打ち消し、`else` の行を 1 段浅いと
    判定していた（2025 の ex12/p3・p5 の参照解答。正しい書き方を崩れと数えていた）。
    """
    assert (
        _broken(
            """
            void show(int a[3][3])
            {
                for (int i = 0; i < 3; i++) {
                    for (int j = 0; j < 3; j++)
                        if (a[i][j] == 1) printf("x");
                        else if (a[i][j] == -1) printf("+");
                        else printf(" ");
                    printf("\\n");
                }
            }
            """
        )
        == []
    )


def test_a_nested_else_pairs_with_the_nearest_if() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                if (a)
                    if (b) x = 1;
                    else x = 2;
                else
                    x = 3;
                if (c) {
                    x = 4;
                } else if (d) {
                    x = 5;
                }
                else
                {
                    x = 6;
                }
                return x;
            }
            """
        )
        == []
    )


def test_an_else_out_of_line_with_its_if_is_broken() -> None:
    assert _broken(
        """
        int main(void)
        {
            for (i = 0; i < n; i++)
                if (a[i] > 0) p++;
            else n++;
            return 0;
        }
        """
    ) == [5]


def test_braces_on_their_own_line_follow_the_statement() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                if (a)
                {
                    b = 1;
                }
                else
                {
                    b = 2;
                }
                do
                {
                    b--;
                } while (b > 0);
                return 0;
            }
            """
        )
        == []
    )


def test_case_labels_may_sit_with_the_switch() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                switch (c) {
                case 1:
                    a = 1;
                    break;
                default:
                    a = 0;
                }
                return a;
            }
            """
        )
        == []
    )


def test_case_labels_may_be_one_level_deeper() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                switch (c) {
                    case 1:
                        a = 1;
                        break;
                    default:
                        a = 0;
                }
                return a;
            }
            """
        )
        == []
    )


def test_a_body_level_with_its_case_label_is_broken() -> None:
    assert _broken(
        """
        int main(void)
        {
            switch (c) {
                case 1:
                a = 1;
                break;
            }
            return a;
        }
        """
    ) == [5, 6]


def test_braces_in_strings_and_comments_do_not_count() -> None:
    assert (
        _broken(
            """
            int main(void)
            {
                printf("{ not a block\\n");  /* } nor this */
                putchar('{');
                // }
                return 0;
            }
            """
        )
        == []
    )


# --------------------------------------------------------------------------
# 数えない行
# --------------------------------------------------------------------------


def test_preprocessor_comments_and_continuations_are_not_counted() -> None:
    report = check_indentation(
        _src(
            """
            #include <stdio.h>
            #define SQUARE(x) \\
                    ((x) * (x))

            int main(void)
            {
            /* 字下げの崩れた
                     コメント */
                  // これも数えない
                printf("%d %d\\n",
                          1, 2);
                int total = 1 +
                        2;
                for (int i = 0;
                         i < 3;
                         i++)
                    total += i;
                return total;
            }
            """
        )
    )
    assert report.broken == ()
    numbers = [line.number for line in report.counted]
    # 折り返した 2 行目以降・コメント・プリプロセッサは数えていない。
    assert numbers == [5, 6, 10, 12, 14, 17, 18, 19]


# --------------------------------------------------------------------------
# 評価器として
# --------------------------------------------------------------------------


def _criterion(levels: int = 3) -> RubricCriterion:
    ladder = [
        RubricLevel(level=0, label="未達", descriptor="揃っていない", score_ratio=0.0),
        RubricLevel(level=1, label="一部", descriptor="一部崩れている", score_ratio=0.5),
        RubricLevel(level=2, label="達成", descriptor="揃っている", score_ratio=1.0),
    ]
    if levels == 2:
        ladder = [ladder[0], ladder[2].model_copy(update={"level": 1})]
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="indentation",
        title="インデント",
        description="入れ子の深さどおりに字下げされているか。",
        weight=1.0,
        levels=tuple(ladder),
        evaluator_id=EVALUATOR_ID,
    )


def _request(
    source: str,
    *,
    criterion: RubricCriterion | None = None,
    filename: str | None = "main.c",
    options: dict[str, object] | None = None,
) -> EvaluationRequest:
    artifact_id = ArtifactId("art_" + "3" * 32)
    payload = source.encode("utf-8")
    artifact = Artifact(
        id=artifact_id,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        storage_key="k",
        content_hash="sha256:0",
        byte_size=len(payload),
        filename=filename,
        created_at=NOW,
    )
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="和を出力せよ。",
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
        artifact_contents={artifact_id: payload},
        options=options or {},
    )


# 10 行数えるうち 1 行（10%）が崩れている。
ONE_IN_TEN = """
int main(void)
{
    int a = 1;
    int b = 2;
    int c = 3;
    int d = 4;
     int e = 5;
    int f = 6;
    return a + b + c + d + e + f;
}
"""


def test_it_is_deterministic_and_conclusive() -> None:
    outcome = CIndentCheck().evaluate(_request(_src(GOOD_4)))
    assert CIndentCheck().kind is EvaluatorKind.DETERMINISTIC
    assert outcome.status is EvaluatorStatus.OK
    score = outcome.scores[0]
    assert score.level == 2 and score.score_ratio == 1.0
    assert score.conclusive is True and score.confidence == 1.0


def test_a_few_broken_lines_take_the_middle_level() -> None:
    outcome = CIndentCheck().evaluate(_request(_src(ONE_IN_TEN)))
    score = outcome.scores[0]
    assert score.level == 1 and score.score_ratio == 0.5
    assert "7 行目" in score.rationale
    assert "7:" in (score.evidence[0].quote or "")


def test_many_broken_lines_take_the_bottom_level() -> None:
    source = "int main(void)\n{\nint a = 1;\nint b = 2;\nreturn a + b;\n}\n"
    assert CIndentCheck().evaluate(_request(source)).scores[0].level == 0


def test_the_threshold_comes_from_the_profile() -> None:
    outcome = CIndentCheck().evaluate(
        _request(_src(ONE_IN_TEN), options={"partial_max_ratio": 0.05})
    )
    assert outcome.scores[0].level == 0


def test_a_two_level_criterion_needs_every_line() -> None:
    outcome = CIndentCheck().evaluate(_request(_src(ONE_IN_TEN), criterion=_criterion(levels=2)))
    assert outcome.scores[0].level == 0


def test_nothing_is_scored_without_a_criterion_naming_it() -> None:
    other = _criterion().model_copy(update={"evaluator_id": "code_test_runner"})
    outcome = CIndentCheck().evaluate(_request(_src(GOOD_4), criterion=other))
    assert outcome.status is EvaluatorStatus.SKIPPED


def test_a_file_that_is_not_c_is_not_judged() -> None:
    """0 点にせず保留にする。出し忘れか読めなかったのかは、ここでは分からない。"""
    outcome = CIndentCheck().evaluate(_request(_src(GOOD_4), filename="main.py"))
    assert outcome.status is EvaluatorStatus.SKIPPED


def test_editor_submissions_without_a_filename_are_judged() -> None:
    outcome = CIndentCheck().evaluate(_request(_src(GOOD_4), filename=None))
    assert outcome.status is EvaluatorStatus.OK


FLAT = """
int main(void)
{
int a = 1;
if (a) {
a = 2;
}
return a;
}
"""


def test_the_rationale_reads_as_japanese() -> None:
    """「単位を4 スペース」「単位をなしと推定」と書かない（2026-10-18 の確認）。"""
    good = CIndentCheck().evaluate(_request(_src(GOOD_4))).scores[0].rationale
    assert "字下げの単位を 4 スペース と推定し" in good

    flat = CIndentCheck().evaluate(_request(_src(FLAT))).scores[0].rationale
    assert "なしと推定" not in flat
    assert "字下げが見つからないため" in flat
