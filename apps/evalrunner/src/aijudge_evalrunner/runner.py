"""ゴールデンセットを採点し、一致度を測って合格基準と突き合わせる。

これが PoC の合否を出す唯一の経路。手で数字を作らない。

このモジュールは合成の中心（composition root）なので、複数のサブシステムに
依存してよい。サブシステムどうしは互いを import しない（ADR 0001）。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from aijudge_analytics import (
    AgreementReport,
    Check,
    Gates,
    Verdict,
    agreement_report,
    evaluate_gates,
    miss_rate,
    overall,
    population_stdev,
    review_rate,
)
from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    GradingRun,
    Routing,
    Submission,
    SubmissionState,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_grading import EvaluatorRegistry, GradingPipeline, SubjectProfile, load_profile

from .golden import GoldenItem

# 提出物の拡張子 → Artifact の種別。増えたらここに足す。
_KINDS: dict[str, ArtifactKind] = {
    ".c": ArtifactKind.CODE,
    ".py": ArtifactKind.CODE,
    ".java": ArtifactKind.CODE,
    ".tex": ArtifactKind.LATEX,
    ".md": ArtifactKind.MARKDOWN,
}

EVALUATOR = UserId("usr_" + "0" * 32)


class ItemResult(BaseModel):
    """1 件ぶんの採点と教員採点の突き合わせ。"""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    key: str
    auto_confirmed: bool
    score_ratio: float
    # 観点コード -> (教員の段階, 機械の段階)。片方しか無い観点は入れない。
    levels: dict[str, tuple[int, int]] = Field(default_factory=dict)
    unscored: tuple[str, ...] = ()
    error: str | None = None

    @property
    def human_changed(self) -> bool:
        """教員が段階を変えたか。見逃し率の材料になる。"""
        return any(human != machine for human, machine in self.levels.values())


class EvalReport(BaseModel):
    """測定結果一式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poc: str
    subject_profile: str
    generated_at: datetime
    item_count: int
    items: tuple[ItemResult, ...] = ()
    agreement: dict[str, AgreementReport] = Field(default_factory=dict)
    observed_miss_rate: float | None = None
    observed_review_rate: float | None = None
    observed_score_stdev: float | None = None
    checks: tuple[Check, ...] = ()
    verdict: Verdict = Verdict.NOT_MEASURED
    errors: tuple[str, ...] = ()


def _submission_from(item: GoldenItem) -> tuple[Submission, bytes]:
    payload = item.source_path.read_bytes()
    kind = _KINDS.get(item.source_path.suffix.lower(), ArtifactKind.MARKDOWN)
    submission_id = SubmissionId(new_id("sub"))
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=kind,
        filename=item.source_path.name,
        storage_key=f"file://{item.source_path}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=datetime.now(UTC),
    )
    submission = Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=datetime.now(UTC),
        submitted_at=datetime.now(UTC),
    )
    return submission, payload


def _task_for(item: GoldenItem, cache: dict[str, TaskVersion]) -> TaskVersion:
    if item.task_name not in cache:
        # 教員が採点した観点をすべて備えた課題として取り込む。
        needs_ai = any(code != "correctness" for code in item.mark.marks)
        cache[item.task_name] = sharif_judge.import_problem(
            item.task_dir,
            subject_profile=item.subject_profile,
            authored_by=EVALUATOR,
            readability_weight=0.3 if needs_ai else 0.0,
        )
    return cache[item.task_name]


def _compare(item: GoldenItem, task: TaskVersion, run: GradingRun) -> ItemResult:
    by_criterion = {score.criterion_id: score for score in run.criterion_scores}
    code_of = {criterion.id: criterion.code for criterion in task.criteria}

    levels: dict[str, tuple[int, int]] = {}
    for criterion_id, score in by_criterion.items():
        code = code_of.get(criterion_id)
        if code is not None and code in item.mark.marks:
            levels[code] = (item.mark.marks[code], score.level)

    return ItemResult(
        key=item.key,
        auto_confirmed=run.routing is Routing.AUTO,
        score_ratio=run.score_ratio,
        levels=levels,
        unscored=tuple(sorted(code_of[cid] for cid in run.unscored_criteria if cid in code_of)),
    )


def run_evaluation(
    items: tuple[GoldenItem, ...],
    *,
    gates: Gates,
    subject_profile: str,
    profile_path: Path,
    registry: EvaluatorRegistry | None = None,
    repeats: int = 1,
    progress: Callable[[str], None] | None = None,
) -> EvalReport:
    """ゴールデンセットを採点して測定する。

    `repeats` を 2 以上にすると、先頭の 1 件を繰り返し採点して
    スコアのばらつき（採点の一貫性）を測る。
    """
    registry = registry or EvaluatorRegistry().load_installed()
    profile: SubjectProfile = load_profile(profile_path, registry)
    pipeline = GradingPipeline(registry, profile)

    tasks: dict[str, TaskVersion] = {}
    results: list[ItemResult] = []
    errors: list[str] = []

    for index, item in enumerate(items, 1):
        if progress:
            progress(f"[{index}/{len(items)}] {item.key}")
        try:
            task = _task_for(item, tasks)
            submission, payload = _submission_from(item)
            run = pipeline.run(task, submission, lambda _, data=payload: data)
        except Exception as exc:
            errors.append(f"{item.key}: {type(exc).__name__}: {exc}")
            continue
        results.append(_compare(item, task, run))

    # --- 観点ごとの一致度 -------------------------------------------------
    pairs: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for result in results:
        for code, pair in result.levels.items():
            pairs[code].append(pair)

    agreement: dict[str, AgreementReport] = {}
    for code, observations in pairs.items():
        levels = _levels_for(code, tasks)
        agreement[code] = agreement_report(
            code,
            [human for human, _ in observations],
            [machine for _, machine in observations],
            levels,
        )

    # --- 全体の指標 -------------------------------------------------------
    observed_review_rate = (
        review_rate([result.auto_confirmed for result in results]) if results else None
    )
    observed_miss = (
        miss_rate(
            [result.auto_confirmed for result in results],
            [result.human_changed for result in results],
        )
        if results
        else None
    )

    stdev: float | None = None
    if repeats > 1 and items:
        stdev = _measure_consistency(items[0], tasks, pipeline, repeats, errors)

    checks = evaluate_gates(
        gates,
        agreement,
        observed_miss_rate=observed_miss,
        observed_review_rate=observed_review_rate,
        observed_score_stdev=stdev,
    )

    return EvalReport(
        poc=gates.poc,
        subject_profile=subject_profile,
        generated_at=datetime.now(UTC),
        item_count=len(results),
        items=tuple(results),
        agreement=agreement,
        observed_miss_rate=observed_miss,
        observed_review_rate=observed_review_rate,
        observed_score_stdev=stdev,
        checks=checks,
        verdict=overall(checks),
        errors=tuple(errors),
    )


def _levels_for(code: str, tasks: dict[str, TaskVersion]) -> tuple[int, ...]:
    """観点コードから採りうる段階を引く。課題をまたいで同じ想定。"""
    for task in tasks.values():
        for criterion in task.criteria:
            if criterion.code == code:
                return tuple(level.level for level in criterion.levels)
    return (0, 1, 2, 3)


def _measure_consistency(
    item: GoldenItem,
    tasks: dict[str, TaskVersion],
    pipeline: GradingPipeline,
    repeats: int,
    errors: list[str],
) -> float | None:
    """同じ提出を繰り返し採点し、総合点のばらつきを測る。"""
    scores: list[float] = []
    for _ in range(repeats):
        try:
            task = _task_for(item, tasks)
            submission, payload = _submission_from(item)
            run = pipeline.run(task, submission, lambda _, data=payload: data)
        except Exception as exc:
            errors.append(f"consistency run: {type(exc).__name__}: {exc}")
            return None
        scores.append(run.score_ratio)
    return population_stdev(scores)
