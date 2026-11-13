"""全体比較のモード（`whole_program`・2026-11-14）が守る約束を固定する。

コメントは自由    コメントを書き換え・足し・消しても「変えていない」。
空白は数えない    字下げ・空行。ただし文字列の中の空白は数える。
コードは全部      コードの行を変える・消す・足すと「変えている」。根拠は最初の食い違い。
宣言は課題ごと    検証データの payload でも、評価器のオプションでも立つ。無ければ従来の目印モード。
入出力の例は見ない 言語名の付いたブロックを問題のプログラムとして使う。
"""

from __future__ import annotations

from datetime import UTC, datetime
from textwrap import dedent

from aijudge_eval_template_diff_check import (
    TemplateDiffCheck,
    check_whole_program,
    find_program,
)

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorStatus,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
)
from aijudge_core import (
    TestCase as Case,
)
from aijudge_core.ids import ArtifactId, CriterionId, SubmissionId, TaskId, TaskVersionId, UserId
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "4" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
ARTIFACT = ArtifactId("art_" + "3" * 32)


def _src(text: str) -> str:
    return dedent(text).lstrip("\n")


PROGRAM = _src(
    """
    #include <stdio.h>

    int main() {
        int a[3] = {3, 1, 2};
        // 先頭から順に繰り返す
        for (int i = 0; i < 3; i++) {
            //
            int min = i;
        }
        printf("%d ", a[0]);
        return 0;
    }
    """
)

STATEMENT = f"""## コメントで説明する

入出力の例:

```
1 2 3
```

次のプログラムの `//` だけの行を書き換えてください。

```c
{PROGRAM}```
"""


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="code_unchanged",
        title="コメント以外を変えていない",
        description="コードの行を変えていないか。",
        weight=1.0,
        evaluator_id="template_diff_check",
        levels=(
            RubricLevel(level=0, label="変えた", descriptor="変えている", score_ratio=0.0),
            RubricLevel(level=1, label="そのまま", descriptor="変えていない", score_ratio=1.0),
        ),
    )


WHOLE = Case(
    name="コメント以外",
    evaluator_id="template_diff_check",
    payload={"whole_program": True},
    hidden=False,
)


def _request(source: str, *, cases: tuple[Case, ...] = (WHOLE,), **options) -> EvaluationRequest:
    payload = source.encode("utf-8")
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement=STATEMENT,
        criteria=(_criterion(),),
        test_cases=cases,
        max_score=10.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=SUBMISSION,
        task_version_id=VERSION,
        learner_id=UserId("usr_" + "7" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=(
            Artifact(
                id=ARTIFACT,
                submission_id=SUBMISSION,
                role=ArtifactRole.ORIGINAL,
                kind=ArtifactKind.CODE,
                storage_key="k",
                content_hash="sha256:x",
                byte_size=len(payload),
                filename="main.c",
                created_at=NOW,
            ),
        ),
        submitted_at=NOW,
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=version,
        submission=submission,
        artifact_contents={ARTIFACT: payload},
        test_cases=cases,
        options=options,
    )


def _level(source: str, **kwargs) -> int:
    outcome = TemplateDiffCheck().evaluate(_request(source, **kwargs))
    assert outcome.status is EvaluatorStatus.OK, outcome.error
    (score,) = outcome.scores
    assert score.conclusive
    return score.level


def test_the_program_is_the_named_block_not_the_io_example() -> None:
    assert find_program(STATEMENT) == PROGRAM


def test_rewriting_and_adding_comments_is_not_a_change() -> None:
    edited = (
        PROGRAM.replace("        //\n", "        // 最小値の位置を i と仮定する\n")
        .replace("// 先頭から順に繰り返す", "/* 書き換えた説明 */")
        .replace("int min = i;", "int min = i;  // 行末のコメント")
    )
    assert _level(edited) == 1


def test_whitespace_outside_strings_is_not_a_change() -> None:
    assert _level(PROGRAM.replace("    ", "\t").replace("int min = i;", "int min=i;")) == 1


def test_a_space_inside_a_string_is_a_change() -> None:
    assert _level(PROGRAM.replace('"%d "', '"%d,"')) == 0


def test_changing_a_code_line_is_a_change_and_points_at_it() -> None:
    source = PROGRAM.replace("int min = i;", "int min = 0;")
    verdict = check_whole_program(source, PROGRAM)
    assert not verdict.kept
    assert verdict.submitted is not None and "int min = 0;" in verdict.submitted.raw
    assert verdict.expected is not None and "int min = i;" in verdict.expected.raw
    outcome = TemplateDiffCheck().evaluate(_request(source))
    assert "コメント以外を変えています" in outcome.scores[0].rationale


def test_adding_or_removing_a_code_line_is_a_change() -> None:
    assert _level(PROGRAM.replace("return 0;", 'printf("x");\n    return 0;')) == 0
    assert _level(PROGRAM.replace('    printf("%d ", a[0]);\n', "")) == 0


def test_commenting_out_code_is_a_change() -> None:
    assert _level(PROGRAM.replace("int min = i;", "// int min = i;")) == 0


def test_the_mode_can_be_set_by_the_evaluator_options() -> None:
    assert _level(PROGRAM.replace("//\n", "// 説明\n"), cases=(), whole_program=True) == 1


def test_without_the_declaration_the_marker_mode_still_applies() -> None:
    """宣言の無い課題は今までどおり目印を探す（この問題文には無いので失敗）。"""
    outcome = TemplateDiffCheck().evaluate(_request(PROGRAM, cases=()))
    assert outcome.status is EvaluatorStatus.FAILED
    assert "ここから" in (outcome.error or "")
