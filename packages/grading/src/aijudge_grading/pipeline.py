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
import logging
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
    GradingPhase,
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
from .registry import EvaluatorRegistry, NormalizerRegistry

logger = logging.getLogger(__name__)

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


class NoDeterministicWork(Exception):
    """この科目には決定的評価器が無い。

    レポート課題のように AI 観点だけで構成される科目では、決定的段階に
    やることが無い。失敗ではないので、呼び出し側は AI 段階へ進める。
    """


class GradingPipeline:
    """科目非依存の採点実行器。"""

    def __init__(
        self,
        registry: EvaluatorRegistry,
        profile: SubjectProfile,
        normalizers: NormalizerRegistry | None = None,
    ) -> None:
        profile.validate_against(registry)
        self._registry = registry
        self._profile = profile
        # 宣言された正規化器だけを解決する。宣言していない科目では
        # レジストリを読みに行かない（起動時の副作用を増やさない）。
        self._normalizers = normalizers
        if profile.normalizers and normalizers is None:
            self._normalizers = NormalizerRegistry().load_installed()
        if profile.normalizers and self._normalizers is not None:
            for name in profile.normalizers:
                self._normalizers.get(name)  # 実在しなければここで落とす

    @property
    def profile(self) -> SubjectProfile:
        return self._profile

    def has_ai_work(self, task_version: TaskVersion, base: GradingRun) -> bool:
        """決定的評価のあとに AI 段階を走らせる意味があるか。

        意味が無いのは 2 つの場合。科目プロファイルが AI 評価器を宣言して
        いない（S6 停止時の劣化動作を含む）か、決定的評価が全観点を確定
        させた（P3 により AI は呼ばれない）か。**どちらでもジョブを積まない**
        ── 積むと、走らせても何も変わらないジョブがキューに溜まる。
        """
        if not self._profile.ai_evaluators:
            return False
        settled = {score.criterion_id for score in base.criterion_scores if score.conclusive}
        return any(c.id not in settled for c in task_version.criteria)

    def run(
        self,
        task_version: TaskVersion,
        submission: Submission,
        load_content: ContentLoader,
        *,
        phase: GradingPhase | None = None,
        base: GradingRun | None = None,
    ) -> GradingRun:
        """採点を走らせる。

        `phase` を渡すとその段階だけを走らせる。**決定的評価は 1 秒未満、
        AI 評価は十数秒**（実測 12.8 秒、うち 95% が LLM）で、同じキューに
        並べると速い方の結果が遅い方の後ろで止まる。分けることで、決定的
        評価の結果が先に返り、AI 評価はあとから届く（設計方針 §9.1・§10）。

        `phase=AI` では `base`（決定的評価の結果）の上に積む。**サンドボックス
        を二度回さない** ── 回すと費用が倍になるうえ、二度目の結果が一度目と
        違いうる（タイムアウト境界の提出）。
        """
        if phase is GradingPhase.AI and base is None:
            raise ValueError("the ai phase needs the deterministic run it builds on")

        contents = {
            artifact.id: load_content(artifact) for artifact in submission.gradable_artifacts
        }
        # --- 1. Normalize（設計方針 §4 step 1）---------------------------
        #
        # **評価器の前に本文へ直す。** PDF や DOCX をそのまま渡すと、AI には
        # バイナリが渡り、字数や節の判定も成立しない。ここで 1 回変換して
        # おけば、構造チェッカーと AI 評価器が同じ本文を見る。
        contents = self._normalize(submission, contents)

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

        if base is not None:
            # 土台の結果を引き継ぐ。**重みはルーブリックから取り直す。**
            # 土台の run では未採点の観点があったぶん重みが比例配分されて
            # おり（`renormalize`）、その値をそのまま使うと最終の集約が狂う。
            results.extend(base.evaluator_results)
            for score in base.criterion_scores:
                try:
                    weight = task_version.criterion(score.criterion_id).weight
                except KeyError:  # pragma: no cover - 課題版が一致しない構成
                    weight = score.weight
                scores.append(score.model_copy(update={"weight": weight}))
            model_ids.update(base.context.model_ids)
            prompt_versions.update(base.context.prompt_versions)

        # --- 2. 決定的評価 -------------------------------------------------
        for evaluator_id in [] if phase is GradingPhase.AI else self._profile.deterministic:
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
        ai_evaluators = [] if phase is GradingPhase.DETERMINISTIC else self._profile.ai_evaluators
        for evaluator_id in ai_evaluators:
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

        if not scores and phase is GradingPhase.DETERMINISTIC and not self._profile.deterministic:
            # 決定的評価器を宣言していない科目（レポート課題など）。この段階
            # では何も出ないのが正しいので、AI 段階に委ねる。
            raise NoDeterministicWork(
                f"profile '{self._profile.name}' declares no deterministic evaluator"
            )
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

    def _normalize(
        self, submission: Submission, contents: dict[ArtifactId, bytes]
    ) -> dict[ArtifactId, bytes]:
        """宣言された正規化器を順に当てる。

        **1 件の失敗で採点を止めない。** 壊れた PDF が 1 つあっても、
        他の提出の採点は続く（失敗した提出は本文が空のまま下流に渡り、
        構造チェッカーが「読めない」と判定して人間に回る）。
        """
        if not self._profile.normalizers or self._normalizers is None:
            return contents
        out = dict(contents)
        for artifact in submission.gradable_artifacts:
            payload = out.get(artifact.id)
            if payload is None:
                continue
            for name in self._profile.normalizers:
                normalizer = self._normalizers.get(name)
                if not normalizer.applies_to(artifact.kind):
                    continue
                try:
                    payload = normalizer.normalize(artifact, payload)
                except Exception:
                    logger.warning(
                        "normalizer %s failed on artifact %s", name, artifact.id, exc_info=True
                    )
            out[artifact.id] = payload
        return out

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
        provisional=run.is_provisional,
        kc_outcomes=run.kc_outcomes,
    )
