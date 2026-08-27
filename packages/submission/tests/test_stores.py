"""保存先の規則を固定する（S3）。

インメモリ実装で緩めると、PostgreSQL 実装に移したときに初めて破綻する。
規則は本番と同じにしておく。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    Routing,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
    TaskVersionId,
    TenantId,
)
from aijudge_submission import (
    FilesystemArtifactStore,
    ImmutabilityViolation,
    SubmissionStoreError,
    artifact_storage_key,
)
from aijudge_submission.memory import InMemoryGradingRunRepository

NOW = datetime(2026, 8, 28, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "1" * 32)


def run(run_id: str) -> GradingRun:
    return GradingRun(
        id=GradingRunId(run_id),
        submission_id=SUBMISSION,
        context=GradingContext(
            task_version_id=TaskVersionId("tsv_" + "2" * 32),
            subject_profile="cs_intro_c",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "3" * 32),
                criterion_id=CriterionId("crt_" + "4" * 32),
                evaluator_result_id=EvaluatorResultId("evr_" + "5" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=3,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="all tests pass",
            ),
        ),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.AUTO,
        created_at=NOW,
    )


# --------------------------------------------------------------------------
# 採点結果は追記のみ（P8）
# --------------------------------------------------------------------------


def test_saving_the_same_run_twice_is_refused() -> None:
    repo = InMemoryGradingRunRepository()
    first = run("grn_" + "a" * 32)
    repo.save(first)
    with pytest.raises(ImmutabilityViolation, match="already exists"):
        repo.save(first)


def test_regrading_adds_a_run_and_keeps_the_old_one() -> None:
    """過去の採点を消さない。異議申し立ての根拠になる。"""
    repo = InMemoryGradingRunRepository()
    old = run("grn_" + "a" * 32)
    new = run("grn_" + "b" * 32)
    repo.save(old)
    repo.save(new)

    assert len(repo.list_for(SUBMISSION)) == 2
    latest = repo.latest_for(SUBMISSION)
    assert latest is not None
    assert latest.id == new.id


def test_superseding_points_the_old_run_at_the_new_one() -> None:
    repo = InMemoryGradingRunRepository()
    old, new = run("grn_" + "a" * 32), run("grn_" + "b" * 32)
    repo.save(old)
    repo.save(new)
    repo.supersede(old.id, new.id)

    stored = repo.get(old.id)
    assert stored is not None
    assert stored.superseded_by == new.id


def test_superseding_twice_is_refused() -> None:
    """二度書き換えられるなら不変ではない。"""
    repo = InMemoryGradingRunRepository()
    old = run("grn_" + "a" * 32)
    repo.save(old)
    repo.supersede(old.id, GradingRunId("grn_" + "b" * 32))
    with pytest.raises(ImmutabilityViolation, match="already superseded"):
        repo.supersede(old.id, GradingRunId("grn_" + "c" * 32))


def test_superseding_an_unknown_run_is_an_error() -> None:
    repo = InMemoryGradingRunRepository()
    with pytest.raises(SubmissionStoreError, match="no GradingRun"):
        repo.supersede(GradingRunId("grn_" + "f" * 32), GradingRunId("grn_" + "e" * 32))


# --------------------------------------------------------------------------
# ファイルストア
# --------------------------------------------------------------------------


def test_a_payload_round_trips(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.put("ten_x/sub_y/art_z/main.c", b"hello")
    assert store.get("ten_x/sub_y/art_z/main.c") == b"hello"
    assert store.exists("ten_x/sub_y/art_z/main.c")


def test_a_key_cannot_escape_the_store(tmp_path: Path) -> None:
    """`..` でストアの外に書けないこと。作る側が正しいのは保証ではない。"""
    store = FilesystemArtifactStore(tmp_path / "store")
    with pytest.raises(SubmissionStoreError, match="escapes the store"):
        store.put("../escaped.txt", b"pwned")
    assert not (tmp_path / "escaped.txt").exists()


def test_an_absolute_key_is_refused(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(SubmissionStoreError, match="invalid storage key"):
        store.put("/etc/passwd", b"pwned")


def test_a_missing_key_is_an_error_not_empty_bytes(tmp_path: Path) -> None:
    """空バイト列を返すと、中身の無い提出が満点でも 0 点でも採点される。"""
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(SubmissionStoreError, match="no artifact stored"):
        store.get("ten_x/sub_y/art_z/main.c")


def test_no_partial_file_is_left_behind(tmp_path: Path) -> None:
    """書き込み途中の中身を読ませない。"""
    store = FilesystemArtifactStore(tmp_path)
    store.put("a/b/c.txt", b"x" * 1024)
    leftovers = [p.name for p in tmp_path.rglob(".*.partial")]
    assert leftovers == []


def test_the_storage_key_starts_with_the_tenant() -> None:
    """テナント単位の移動・削除・容量計算をプレフィックス走査で済ませる。"""
    key = artifact_storage_key(
        TenantId("ten_1"), SubmissionId("sub_2"), ArtifactId("art_3"), "main.c"
    )
    assert key == "ten_1/sub_2/art_3/main.c"
    assert key.startswith("ten_1/")
