"""配列の宣言の判定が守る約束を固定する（2026-11-13）。

固定したいのは 4 つ。

宣言は全部数える    大きさが変数・省略・多次元・修飾子付き・for の初期化部・複数の宣言子。
数えないものがある  定数とコメントの中・添字で読むだけ・初期化子・関数の引数・#define。
段階は有無で決まる  無ければ最上段、あれば最下段。決定的で確定。根拠は宣言の行。
ソースが無ければ    0 点にせず SKIPPED。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from textwrap import dedent

from aijudge_eval_c_array_check import EVALUATOR_ID, CArrayCheck, find_arrays

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
from aijudge_core.ids import ArtifactId, CriterionId, SubmissionId, TaskId, TaskVersionId, UserId
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def _src(text: str) -> str:
    return dedent(text).lstrip("\n")


def _lines(text: str) -> list[int]:
    return [a.line for a in find_arrays(_src(text))]


# 配列を使わない解（exam08 p6 の参照解答と同じ形）。
WITHOUT = _src(
    """
    #include <stdio.h>
    #define N 1000

    int main(int argc, char *argv[]) {
        int n, x;
        scanf("%d", &n);   // a[0] のような書き方はしない
        scanf("%d", &x);
        int max = x, second = x;  /* second[0] でもない */
        for (int i = 1; i < n; i++) {
            scanf("%d", &x);
            if (x > max) {
                second = max;
                max = x;
            }
        }
        printf("max[%d] second '[' %d\\n", max, second);
        return 0;
    }
    """
)


# --------------------------------------------------------------------------
# 宣言は全部数える
# --------------------------------------------------------------------------


def test_a_solution_without_arrays_has_none() -> None:
    assert find_arrays(WITHOUT) == ()


def test_a_plain_array_declaration_is_found_with_its_line() -> None:
    (found,) = find_arrays(_src("int main(void) {\n    int a[1000];\n    return 0;\n}\n"))
    assert (found.line, found.text) == (2, "int a[1000];")


def test_every_form_of_array_declaration_counts() -> None:
    assert _lines(
        """
        int main(void) {
            int n = 3;
            int v[n];
            char s[] = "abc";
            double m[2][3];
            static int keep[10];
            unsigned long big[4] = {0};
            int x, y[5], z;
            int *p[2];
            struct point { int x; int y; } pts[3];
            for (int t[2] = {0, 0}; t[0] < 1; t[0]++) {}
            return 0;
        }
        """
    ) == [3, 4, 5, 6, 7, 8, 9, 10, 11]


# --------------------------------------------------------------------------
# 数えないものがある
# --------------------------------------------------------------------------


def test_reading_by_index_initializers_and_parameters_do_not_count() -> None:
    assert (
        _lines(
            """
            int first(int values[], int n) {
                return values[0];
            }
            int main(int argc, char *argv[]) {
                int x = argv[0][0];
                x = x + sizeof(int[3]);
                return first(0, x);
            }
            """
        )
        == []
    )


def test_brackets_inside_comments_strings_and_macros_do_not_count() -> None:
    assert (
        _lines(
            """
            #define SIZE(a) (sizeof(a) / sizeof(a[0]))
            int main(void) {
                // int a[10];
                /* int b[10];
                   int c[10]; */
                printf("int d[10];\\n");
                char q = '[';
                return 0;
            }
            """
        )
        == []
    )


# --------------------------------------------------------------------------
# 評価器として
# --------------------------------------------------------------------------


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="no_array",
        title="配列を使っていないか",
        description="配列を使わず、通常の変数だけで求めているか。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="配列を使っている", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="配列を使っていない", score_ratio=1.0),
        ),
        evaluator_id=EVALUATOR_ID,
    )


def _request(source: str | None, *, criterion: RubricCriterion | None = None) -> EvaluationRequest:
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
        statement="最大と 2 番目を求めよ。",
        criteria=(criterion or _criterion(),),
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
    )


def test_no_array_gets_the_top_level_conclusively() -> None:
    outcome = CArrayCheck().evaluate(_request(WITHOUT))
    assert outcome.status is EvaluatorStatus.OK
    (score,) = outcome.scores
    assert (score.level, score.score_ratio) == (1, 1.0)
    assert score.conclusive and score.confidence == 1.0
    assert score.kind is EvaluatorKind.DETERMINISTIC
    assert outcome.raw_output["arrays"] == []


def test_an_array_gets_the_bottom_level_with_the_line_as_evidence() -> None:
    source = WITHOUT.replace("    int n, x;\n", "    int n, x;\n    int a[1000];\n")
    outcome = CArrayCheck().evaluate(_request(source))
    (score,) = outcome.scores
    assert (score.level, score.score_ratio) == (0, 0.0)
    assert "6 行目「int a[1000];」" in score.rationale
    (evidence,) = score.evidence
    assert evidence.span == LineSpan(start_line=6, end_line=6)


def test_no_source_is_skipped_not_zero() -> None:
    outcome = CArrayCheck().evaluate(_request(None))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


def test_a_task_without_the_criterion_is_skipped() -> None:
    other = _criterion().model_copy(update={"evaluator_id": "code_test_runner"})
    assert CArrayCheck().evaluate(_request(WITHOUT, criterion=other)).status is (
        EvaluatorStatus.SKIPPED
    )


def test_the_evaluator_is_registered_as_an_entry_point() -> None:
    names = {ep.name for ep in entry_points(group="aijudge.evaluators")}
    assert EVALUATOR_ID in names
