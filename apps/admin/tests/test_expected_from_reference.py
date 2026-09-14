"""期待出力は**参照解答を走らせて**作る（#305）。

固定したいのは 4 つ。

実際に走らせる     モデルが書いた期待出力は使わない。誤っていても誰も
                   気づかないまま決定的採点に入り、決定的な結果は
                   `conclusive` なので AI にも見直されない（P3）。誤りは
                   「全員が落ちる」として現れ、原因は提出物の側に見える。
採点と同じ経路で   別に実行系を書くと、ここで作った期待出力を採点が
                   再現しない。
落ちたら採らない   非ゼロ終了やタイムアウトの途中までの出力を期待出力に
                   すると、正しい提出がその中途半端な出力と比べられる。
空と失敗を分ける   出力が空なのか走らなかったのかを混ぜると、「空を期待
                   する」テストケースが黙って出来上がる。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin.task_verifier import outputs_for
from aijudge_core import Provenance, RubricCriterion, RubricLevel, TaskVersion
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId
from aijudge_grading import EvaluatorRegistry, load_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE = load_profile(REPO_ROOT / "subjects" / "cs_lang_c_intro.yaml")
VERSION = TaskVersionId("tsv_" + "1" * 32)

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

SUM = """#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d %d", &a, &b) != 2) return 1;
    printf("%d\\n", a + b);
    return 0;
}
"""

REFUSES = """#include <stdio.h>
int main(void) {
    printf("half");
    return 3;
}
"""

BROKEN = "int main(void) { this is not C }\n"


def _version() -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "2" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="## 合計 ##\n\n2 つの整数の和を出力する。",
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "3" * 32),
                code="correctness",
                title="出力の正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
                evaluator_id="code_test_runner",
            ),
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "4" * 32)),
        created_at=datetime(2026, 9, 15, tzinfo=UTC),
    )


def _run(reference: str, inputs: list[tuple[str, str]]):
    return outputs_for(
        EvaluatorRegistry().load_installed(),
        PROFILE,
        _version(),
        reference,
        inputs,
        evaluator_id="code_test_runner",
    )


@needs_c_compiler
def test_the_expected_output_is_what_the_reference_actually_printed() -> None:
    runs = _run(SUM, [("case1", "1 2\n"), ("case2", "10 -4\n")])

    assert [r.name for r in runs] == ["case1", "case2"]
    assert [r.output for r in runs] == ["3\n", "6\n"]
    assert all(r.ok for r in runs)


@needs_c_compiler
def test_a_case_that_did_not_finish_is_not_adopted() -> None:
    """**落ちたら採らない。** 途中まで出た出力を期待出力にしない。"""
    runs = _run(REFUSES, [("case1", "1 2\n")])

    assert not runs[0].ok
    assert runs[0].reason, "理由を言わないと、なぜ採れないのか画面から読めない"


@needs_c_compiler
def test_a_reference_that_does_not_build_yields_nothing() -> None:
    """**1 件も作らない。** コンパイルできない解答例からは期待出力が作れない。"""
    runs = _run(BROKEN, [("case1", "1 2\n"), ("case2", "3 4\n")])

    assert len(runs) == 2
    assert not any(r.ok for r in runs)
    assert all(r.output == "" for r in runs)


def test_no_inputs_runs_nothing() -> None:
    """**入力が無ければサンドボックスも起こさない。** 空の実行に費用を払わない。"""
    assert _run(SUM, []) == ()
