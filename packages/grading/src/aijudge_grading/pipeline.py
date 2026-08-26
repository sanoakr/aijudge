"""採点パイプライン（設計方針 §04）。

このモジュールは科目を知らない。知っているのは
「プロファイルが並べた Evaluator を順に呼び、結果を集約する」ことだけ。
科目固有の知識が 1 行でもここに入ったら ADR 0002 の前提が崩れている。

    Normalize → Deterministic → AI → Aggregate → Route → Publish

現状 Normalize と AI は空で通る。AI 評価器が 1 つも登録されていなくても
決定的評価だけで採点が完結する（＝ S6 停止時の劣化動作、設計原則 P2）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from aijudge_core import (
    Artifact,
    CriterionScore,
    EvaluatorKind,
    EvaluatorResult,
    EvaluatorStatus,
    GradingCompleted,
    GradingContext,
    GradingRun,
    KcOutcome,
    Routing,
    Submission,
    TaskVersion,
    aggregate,
    new_id,
    renormalize,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionScoreId,
    EvaluatorResultId,
    EventId,
    GradingRunId,
    TenantId,
)

from .profile import SubjectProfile
from .protocol import EvaluationOutcome, EvaluationRequest
from .registry import EvaluatorRegistry

PIPELINE_VERSION = "0.1.0"

ContentLoader = Callable[[Artifact], bytes]


def compute_input_hash(submission: Submission, contents: dict[ArtifactId, bytes]) -> str:
    """再採点の同一性判定に使う入力ハッシュ（設計原則 P8）。

    これは「同じ中身か」を表す値であって「同じ提出か」ではない。
    ArtifactId は提出ごとに新しく振られるので、含めてはならない
    （含めると同一内容の再採点でも別のハッシュになり、
    モデル更新時に「結果が変わったのは入力が違うからか」を切り分けられなくなる）。

    畳むのは役割と中身だけ。内容が 1 バイトでも変われば値が変わる。
    """
    digest = hashlib.sha256()
    entries = sorted(
        (artifact.role.value, hashlib.sha256(contents.get(artifact.id, b"")).hexdigest())
        for artifact in submission.gradable_artifacts
    )
    for role, content_digest in entries:
        digest.update(role.encode())
        digest.update(b"\x00")
        digest.update(content_digest.encode())
        digest.update(b"\x1e")
    return f"sha256:{digest.hexdigest()}"


def derive_kc_outcomes(
    task_version: TaskVersion, scores: tuple[CriterionScore, ...]
) -> tuple[KcOutcome, ...]:
    """観点別スコアを KC 単位に畳む（設計原則 P6）。

    現状は Q-matrix が Task 単位なので、課題の総合比率を各 KC に割り当てる。
    観点と KC を直接対応づける粒度は PoC-2 の課題（そのときは QMatrixEntry に
    criterion_id を足す）。ここを先に作り込まないのは、実データで
    その粒度が必要かどうかを確かめてから決めたいため。
    """
    if not task_version.q_matrix or not scores:
        return ()

    total_ratio, confidence = aggregate(scores)
    score_ids: tuple[CriterionScoreId, ...] = tuple(score.id for score in scores)
    # QMatrixEntry.weight はまだ使っていない。使うべきかどうかは
    # 観点 × KC の粒度を実データで確かめてから決める。
    return tuple(
        KcOutcome(
            kc_id=entry.kc_id,
            score_ratio=total_ratio,
            confidence=confidence,
            criterion_score_ids=score_ids,
        )
        for entry in task_version.q_matrix
    )


class GradingPipeline:
    """科目非依存の採点実行器。"""

    def __init__(self, registry: EvaluatorRegistry, profile: SubjectProfile) -> None:
        profile.validate_against(registry)
        self._registry = registry
        self._profile = profile

    @property
    def profile(self) -> SubjectProfile:
        return self._profile

    def run(
        self,
        task_version: TaskVersion,
        submission: Submission,
        load_content: ContentLoader,
    ) -> GradingRun:
        contents = {
            artifact.id: load_content(artifact) for artifact in submission.gradable_artifacts
        }

        results: list[EvaluatorResult] = []
        scores: list[CriterionScore] = []
        # 再現性のための出所（P8）。どの評価器がどのモデル・どのプロンプト版で
        # 出したのかを GradingContext に残す。
        model_ids: dict[str, str] = {}
        prompt_versions: dict[str, str] = {}

        def record_provenance(evaluator_id: str, outcome: EvaluationOutcome) -> None:
            if outcome.model_id:
                model_ids[evaluator_id] = outcome.model_id
            if outcome.prompt_id:
                prompt_versions[evaluator_id] = outcome.prompt_id

        # --- 2. 決定的評価 -------------------------------------------------
        for evaluator_id in self._profile.deterministic:
            outcome = self._invoke(
                evaluator_id,
                EvaluationRequest(
                    task_version=task_version,
                    submission=submission,
                    artifact_contents=contents,
                    test_cases=self._test_cases_for(task_version, evaluator_id),
                    timeout_seconds=self._profile.timeout_seconds,
                    options=self._profile.evaluator_options.get(evaluator_id, {}),
                ),
            )
            record_provenance(evaluator_id, outcome)
            results.append(self._to_result(evaluator_id, EvaluatorKind.DETERMINISTIC, outcome))
            scores.extend(self._attach(outcome, results[-1].id))

        settled = {score.criterion_id for score in scores if score.conclusive}

        # --- 3. AI 評価（ルーブリック観点ごとに 1 回） -----------------------
        for evaluator_id in self._profile.ai_evaluators:
            for criterion in task_version.criteria:
                # 決定的評価が確定させた観点は AI に問い合わせない（P3）。
                # 呼ばないので費用も掛からない。
                if criterion.id in settled:
                    continue
                if criterion.evaluator_id not in (None, evaluator_id):
                    continue
                outcome = self._invoke(
                    evaluator_id,
                    EvaluationRequest(
                        task_version=task_version,
                        submission=submission,
                        artifact_contents=contents,
                        criterion=criterion,
                        prior_results=tuple(scores),
                        timeout_seconds=self._profile.timeout_seconds,
                        options=self._profile.evaluator_options.get(evaluator_id, {}),
                    ),
                )
                record_provenance(evaluator_id, outcome)
                results.append(self._to_result(evaluator_id, EvaluatorKind.AI, outcome))
                scores.extend(self._attach(outcome, results[-1].id))

        if not scores:
            raise RuntimeError(
                f"no evaluator produced a score for submission {submission.id!r}; "
                f"check the '{self._profile.name}' profile"
            )

        # --- 4. 集約 / 5. 振り分け ------------------------------------------
        # 評価器が落ちた観点があると、残りの重みは 1.0 に満たない。
        # 0 点にすれば学習者に不当な不利益が出るし、満点にすれば
        # 誰も見ていない観点に点を与えることになる。どちらも取らず、
        # 採点できた観点で暫定の点を出し、必ず人間のレビューへ回す（P5）。
        scored = {score.criterion_id for score in scores}
        unscored = tuple(c.id for c in task_version.criteria if c.id not in scored)

        final = renormalize(tuple(scores)) if unscored else tuple(scores)
        score_ratio, confidence = aggregate(final)
        routing = (
            Routing.REVIEW_REQUIRED
            if unscored or self._profile.review_policy.requires_review(final, score_ratio)
            else Routing.AUTO
        )

        return GradingRun(
            id=GradingRunId(new_id("grn")),
            submission_id=submission.id,
            context=GradingContext(
                task_version_id=task_version.id,
                subject_profile=self._profile.name,
                rubric_version=f"{task_version.id}@{task_version.version}",
                input_hash=compute_input_hash(submission, contents),
                prompt_versions=prompt_versions,
                model_ids=model_ids,
                pipeline_version=PIPELINE_VERSION,
            ),
            evaluator_results=tuple(results),
            criterion_scores=final,
            kc_outcomes=derive_kc_outcomes(task_version, final),
            score_ratio=score_ratio,
            confidence=confidence,
            routing=routing,
            unscored_criteria=unscored,
            created_at=datetime.now(UTC),
        )

    # -- internals ---------------------------------------------------------

    def _test_cases_for(self, task_version: TaskVersion, evaluator_id: str) -> tuple:
        return tuple(case for case in task_version.test_cases if case.evaluator_id == evaluator_id)

    def _invoke(self, evaluator_id: str, request: EvaluationRequest) -> EvaluationOutcome:
        """評価器 1 個を呼ぶ。落ちても採点全体は落とさない（§04 step 2）。"""
        evaluator = self._registry.get(evaluator_id)
        try:
            return evaluator.evaluate(request)
        except Exception as exc:
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _to_result(
        self, evaluator_id: str, kind: EvaluatorKind, outcome: EvaluationOutcome
    ) -> EvaluatorResult:
        return EvaluatorResult(
            id=EvaluatorResultId(new_id("evr")),
            evaluator_id=evaluator_id,
            kind=kind,
            status=outcome.status,
            raw_output=outcome.raw_output,
            error=outcome.error,
        )

    def _attach(
        self, outcome: EvaluationOutcome, result_id: EvaluatorResultId
    ) -> tuple[CriterionScore, ...]:
        """Evaluator が採番できない EvaluatorResult への参照を張り直す。"""
        return tuple(
            score.model_copy(update={"evaluator_result_id": result_id}) for score in outcome.scores
        )


def grading_completed_event(
    run: GradingRun, submission: Submission, *, tenant_id: TenantId
) -> GradingCompleted:
    """採点結果を S7 / S9 へ渡すイベントに変換する。"""
    return GradingCompleted(
        event_id=EventId(new_id("evt")),
        tenant_id=tenant_id,
        occurred_at=run.created_at,
        grading_run_id=run.id,
        submission_id=run.submission_id,
        task_version_id=run.context.task_version_id,
        learner_id=submission.learner_id,
        score_ratio=run.score_ratio,
        confidence=run.confidence,
        routing=run.routing,
        kc_outcomes=run.kc_outcomes,
    )
