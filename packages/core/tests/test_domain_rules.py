"""コアが守るべき規則をテストとして固定する。

ここのテストが落ちるときは、実装のバグか設計方針の変更のどちらかであり、
後者なら ADR を書き足してからテストを直す。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    CharSpan,
    CriterionScore,
    EvaluatorKind,
    Evidence,
    KnowledgeComponent,
    LineSpan,
    RegionSpan,
    ReviewPolicy,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TranscriptionMeta,
    aggregate,
    assert_transition,
    can_transition,
    new_id,
    resolve_conflicts,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    KcId,
    SubmissionId,
    TaskVersionId,
    UserId,
)

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# ID
# --------------------------------------------------------------------------


def test_new_id_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError, match="unknown id prefix"):
        new_id("nope")


# --------------------------------------------------------------------------
# ArtifactSpan — 3 モダリティが同じ型で表現できること
# --------------------------------------------------------------------------


def test_spans_cover_code_text_and_image() -> None:
    spans = [
        LineSpan(start_line=12, end_line=18),
        CharSpan(start=0, end=42),
        RegionSpan(page=0, x=0.1, y=0.2, width=0.5, height=0.3),
    ]
    assert [span.kind for span in spans] == ["line", "char", "region"]


def test_region_span_must_stay_inside_the_artifact() -> None:
    with pytest.raises(ValidationError):
        RegionSpan(x=0.8, y=0.0, width=0.5, height=0.1)


def test_evidence_is_invalidated_when_the_artifact_changes() -> None:
    artifact_id = ArtifactId(new_id("art"))
    evidence = Evidence(
        artifact_id=artifact_id,
        artifact_content_hash="sha256:aaa",
        span=LineSpan(start_line=3, end_line=3),
        quote="return n * factorial(n - 1)",
    )
    assert evidence.targets(artifact_id, "sha256:aaa")
    # 再正規化で内容が変われば、根拠は黙って別の場所を指さずに無効になる。
    assert not evidence.targets(artifact_id, "sha256:bbb")


# --------------------------------------------------------------------------
# KnowledgeComponent
# --------------------------------------------------------------------------


def test_kc_key_is_the_canonical_dotted_path() -> None:
    kc = KnowledgeComponent(
        id=KcId(new_id("kc")),
        namespace="math",
        path=("calculus", "integration", "substitution"),
        label="置換積分",
    )
    assert kc.key == "math.calculus.integration.substitution"
    assert kc.depth == 3


def test_kc_hierarchy_does_not_cross_namespaces() -> None:
    parent = KnowledgeComponent(
        id=KcId(new_id("kc")), namespace="math", path=("calculus",), label="微積分"
    )
    child = KnowledgeComponent(
        id=KcId(new_id("kc")),
        namespace="math",
        path=("calculus", "integration"),
        label="積分",
    )
    stranger = KnowledgeComponent(
        id=KcId(new_id("kc")),
        namespace="phys",
        path=("calculus", "integration"),
        label="積分（物理）",
    )
    assert child.is_descendant_of(parent)
    assert not stranger.is_descendant_of(parent)


def test_kc_path_segments_are_validated() -> None:
    with pytest.raises(ValidationError):
        KnowledgeComponent(id=KcId(new_id("kc")), namespace="math", path=("Calculus",), label="x")


# --------------------------------------------------------------------------
# ルーブリック
# --------------------------------------------------------------------------


def _criterion(code: str, weight: float) -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId(new_id("crt")),
        code=code,
        title=code,
        description="…",
        weight=weight,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="要件を満たさない", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="要件を満たす", score_ratio=1.0),
        ),
    )


def test_top_rubric_level_must_be_full_credit() -> None:
    with pytest.raises(ValidationError):
        RubricCriterion(
            id=CriterionId(new_id("crt")),
            code="c1",
            title="c1",
            description="…",
            weight=1.0,
            levels=(
                RubricLevel(level=0, label="未達", descriptor="x", score_ratio=0.0),
                RubricLevel(level=1, label="一部", descriptor="y", score_ratio=0.5),
            ),
        )


# --------------------------------------------------------------------------
# 提出フローの状態機械（手書き提出）
# --------------------------------------------------------------------------


def test_text_submission_skips_the_transcription_states() -> None:
    assert can_transition(SubmissionState.DRAFT, SubmissionState.SUBMITTED)


def test_handwriting_must_pass_through_confirmation() -> None:
    assert can_transition(SubmissionState.DRAFT, SubmissionState.TRANSCRIBING)
    assert not can_transition(SubmissionState.TRANSCRIBING, SubmissionState.SUBMITTED)
    assert can_transition(SubmissionState.TRANSCRIBING, SubmissionState.AWAITING_CONFIRMATION)
    assert can_transition(SubmissionState.AWAITING_CONFIRMATION, SubmissionState.SUBMITTED)


def test_submitted_is_terminal() -> None:
    with pytest.raises(ValueError, match="illegal submission transition"):
        assert_transition(SubmissionState.SUBMITTED, SubmissionState.DRAFT)


def _transcription(submission_id: SubmissionId, *, confirmed: bool) -> tuple[Artifact, Artifact]:
    original = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.IMAGE,
        storage_key="s3://raw/page1.jpg",
        content_hash="sha256:img",
        byte_size=1024,
        created_at=NOW,
    )
    meta = TranscriptionMeta(
        engine="vlm",
        model_id="local-vlm-v1",
        confidence_map={"eq1": 0.62},
        confirmed_at=NOW if confirmed else None,
        confirmed_by=UserId(new_id("usr")) if confirmed else None,
    )
    transcription = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.TRANSCRIPTION,
        kind=ArtifactKind.LATEX,
        storage_key="s3://txt/page1.tex",
        content_hash="sha256:tex",
        byte_size=256,
        derived_from=original.id,
        transcription=meta,
        created_at=NOW,
    )
    return original, transcription


def test_unconfirmed_transcription_cannot_be_submitted() -> None:
    """確認ステップを迂回して提出できる抜け道を型で塞ぐ（PoC-3.5 の合格基準）。"""
    submission_id = SubmissionId(new_id("sub"))
    artifacts = _transcription(submission_id, confirmed=False)
    with pytest.raises(ValidationError, match="must be confirmed"):
        Submission(
            id=submission_id,
            task_version_id=TaskVersionId(new_id("tsv")),
            learner_id=UserId(new_id("usr")),
            state=SubmissionState.SUBMITTED,
            artifacts=artifacts,
            created_at=NOW,
            submitted_at=NOW,
        )


def test_confirmed_transcription_is_gradable_and_sets_the_deadline_timestamp() -> None:
    submission_id = SubmissionId(new_id("sub"))
    artifacts = _transcription(submission_id, confirmed=True)
    submission = Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=artifacts,
        created_at=NOW,
        submitted_at=NOW,
    )
    # 原本画像と確定テキストの両方が評価器へ渡る。
    assert len(submission.gradable_artifacts) == 2
    assert submission.deadline_timestamp == NOW


def test_transcription_artifact_requires_provenance() -> None:
    with pytest.raises(ValidationError, match="derived_from"):
        Artifact(
            id=ArtifactId(new_id("art")),
            submission_id=SubmissionId(new_id("sub")),
            role=ArtifactRole.TRANSCRIPTION,
            kind=ArtifactKind.LATEX,
            storage_key="s3://txt/x.tex",
            content_hash="sha256:tex",
            byte_size=1,
            created_at=NOW,
        )


# --------------------------------------------------------------------------
# 採点 — P3 / P4 / P8
# --------------------------------------------------------------------------


def _score(
    *,
    criterion_id: CriterionId,
    kind: EvaluatorKind,
    ratio: float,
    weight: float,
    confidence: float,
    conclusive: bool = False,
) -> CriterionScore:
    evidence: tuple[Evidence, ...] = ()
    if kind is EvaluatorKind.AI:
        evidence = (
            Evidence(
                artifact_id=ArtifactId(new_id("art")),
                artifact_content_hash="sha256:x",
                span=CharSpan(start=0, end=10),
            ),
        )
    return CriterionScore(
        id=CriterionScoreId(new_id("cs")),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId(new_id("evr")),
        kind=kind,
        level=1,
        score_ratio=ratio,
        weight=weight,
        confidence=confidence,
        conclusive=conclusive,
        evidence=evidence,
        rationale="…",
    )


def test_only_deterministic_evaluators_may_be_conclusive() -> None:
    with pytest.raises(ValidationError, match="conclusive"):
        _score(
            criterion_id=CriterionId(new_id("crt")),
            kind=EvaluatorKind.AI,
            ratio=1.0,
            weight=1.0,
            confidence=0.9,
            conclusive=True,
        )


def test_deterministic_result_wins_over_ai_on_the_same_criterion() -> None:
    """P3: 決定的評価が確定させた観点を AI で覆さない。"""
    criterion_id = CriterionId(new_id("crt"))
    deterministic = _score(
        criterion_id=criterion_id,
        kind=EvaluatorKind.DETERMINISTIC,
        ratio=0.0,
        weight=1.0,
        confidence=1.0,
        conclusive=True,
    )
    ai = _score(
        criterion_id=criterion_id,
        kind=EvaluatorKind.AI,
        ratio=1.0,
        weight=1.0,
        confidence=0.8,
    )
    resolved = resolve_conflicts((ai, deterministic))
    assert len(resolved) == 1
    assert resolved[0].id == deterministic.id
    assert resolved[0].score_ratio == 0.0


def test_aggregate_uses_the_weakest_uncertain_confidence() -> None:
    """一つでも自信のない観点があれば、採点全体の確信度をそれに引きずらせる。"""
    scores = (
        _score(
            criterion_id=CriterionId(new_id("crt")),
            kind=EvaluatorKind.DETERMINISTIC,
            ratio=1.0,
            weight=0.5,
            confidence=1.0,
            conclusive=True,
        ),
        _score(
            criterion_id=CriterionId(new_id("crt")),
            kind=EvaluatorKind.AI,
            ratio=0.5,
            weight=0.3,
            confidence=0.62,
        ),
        _score(
            criterion_id=CriterionId(new_id("crt")),
            kind=EvaluatorKind.AI,
            ratio=1.0,
            weight=0.2,
            confidence=0.91,
        ),
    )
    total, confidence = aggregate(scores)
    assert total == pytest.approx(0.85)
    assert confidence == pytest.approx(0.62)


def test_aggregate_rejects_weights_that_do_not_sum_to_one() -> None:
    scores = (
        _score(
            criterion_id=CriterionId(new_id("crt")),
            kind=EvaluatorKind.DETERMINISTIC,
            ratio=1.0,
            weight=0.5,
            confidence=1.0,
        ),
    )
    with pytest.raises(ValueError, match=r"must sum to 1\.0"):
        aggregate(scores)


def test_review_policy_ignores_conclusive_scores() -> None:
    """決定的に確定した観点は、確信度が低くてもレビュー行きの理由にしない。"""
    policy = ReviewPolicy(confidence_below=0.75)
    conclusive = _score(
        criterion_id=CriterionId(new_id("crt")),
        kind=EvaluatorKind.DETERMINISTIC,
        ratio=0.0,
        weight=1.0,
        confidence=0.1,
        conclusive=True,
    )
    assert not policy.requires_review((conclusive,), total_ratio=0.0)


def test_review_policy_catches_the_pass_fail_boundary() -> None:
    policy = ReviewPolicy(confidence_below=0.0, boundary_score=0.6, boundary_margin=0.05)
    score = _score(
        criterion_id=CriterionId(new_id("crt")),
        kind=EvaluatorKind.AI,
        ratio=0.58,
        weight=1.0,
        confidence=0.99,
    )
    assert policy.requires_review((score,), total_ratio=0.58)
    assert not policy.requires_review((score,), total_ratio=0.90)
