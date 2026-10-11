"""正しさ 0.5 → インデント 0.5 を AND で組んだ課題を、実際のパイプラインで採点する。

prog2 第 4 回以降の形（2026-10-11）。確かめたいのは 3 つ。

- 動いて字下げも揃っていれば満点
- 動くが字下げが崩れていれば、正しさの分だけ（インデントは 0%）
- 動かなければそこで止まる ── インデントは評価せず 0% として数える（`skipped_criteria`）
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Aggregation,
    Artifact,
    ArtifactKind,
    ArtifactRole,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    new_id,
)
from aijudge_core.ids import ArtifactId, CourseId, CriterionId, SubmissionId, TaskVersionId, UserId
from aijudge_grading import GradingPipeline, default_registry, load_profile

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "evals" / "fixtures" / "prog2-2025-ex06-p3"
PROFILE_PATH = REPO_ROOT / "subjects" / "cs_lang_c_intro.yaml"
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

INDENTATION = RubricCriterion(
    id=CriterionId("crt_" + "9" * 32),
    code="indentation",
    title="インデント",
    description="入れ子の深さどおりに字下げされているか。",
    weight=0.5,
    levels=(
        RubricLevel(level=0, label="未達", descriptor="揃っていない", score_ratio=0.0),
        RubricLevel(level=1, label="一部", descriptor="一部崩れている", score_ratio=0.5),
        RubricLevel(level=2, label="達成", descriptor="揃っている", score_ratio=1.0),
    ),
    evaluator_id="c_indent_check",
)


@pytest.fixture(scope="module")
def task_version():
    imported = sharif_judge.import_problem(
        FIXTURE,
        course_id=CourseId("crs_" + "0" * 32),
        subject_profile="cs_lang_c_intro",
        authored_by=UserId(new_id("usr")),
    )
    correctness = imported.criteria[0].model_copy(
        update={"weight": 0.5, "evaluator_id": "code_test_runner"}
    )
    return imported.model_copy(
        update={"criteria": (correctness, INDENTATION), "aggregation": Aggregation.AND}
    )


def _grade(source: str, task_version):  # type: ignore[no-untyped-def]
    submission_id = SubmissionId(new_id("sub"))
    payload = source.encode()
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        filename="maxmin.c",
        storage_key=f"memory://{submission_id}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=NOW,
    )
    submission = Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=NOW,
        submitted_at=NOW,
    )
    registry = default_registry()
    pipeline = GradingPipeline(registry, load_profile(PROFILE_PATH, registry))
    # 集約は worker が課題とコースから決めて渡す（`effective_aggregation`）。ここでは課題の値。
    return pipeline.run(
        task_version,
        submission,
        lambda _artifact: payload,
        aggregation=task_version.aggregation or Aggregation.OR,
    )


def _flatten(source: str) -> str:
    """字下げを全部落とす（動きは変わらない）。"""
    return "\n".join(line.lstrip() for line in source.splitlines()) + "\n"


def test_working_and_indented_scores_full_marks(task_version) -> None:
    run = _grade(task_version.reference_solution, task_version)
    assert run.score_ratio == pytest.approx(1.0)
    assert run.skipped_criteria == ()


def test_working_but_unindented_keeps_only_the_correctness(task_version) -> None:
    run = _grade(_flatten(task_version.reference_solution), task_version)
    indentation = next(s for s in run.criterion_scores if s.criterion_id == INDENTATION.id)
    assert indentation.score_ratio == 0.0
    assert run.score_ratio == pytest.approx(0.5)


def test_not_compiling_stops_before_the_indentation(task_version) -> None:
    run = _grade("int main(void) { return }\n", task_version)
    assert run.score_ratio == pytest.approx(0.0)
    # 評価しなかった観点として分けて記録される（未採点ではない・ADR 0015）。
    assert INDENTATION.id in run.skipped_criteria
