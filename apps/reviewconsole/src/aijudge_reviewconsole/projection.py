"""採点とレビューの記録から、測定用の観測レコードを投影する。

**向きが要点。** 採点側は測定を知らない。ここは採点の結果を読んで
観測を書き出す片方向の変換で、失敗しても採点には影響しない（ADR 0007）。

投影をここで行う理由は、必要な材料が揃っている場所がここだけだから。
観点コードと段階の集合は課題定義（`TaskVersion`）にあり、判定は
`GradingRun` にあり、教員の段階は `GoldenMark` と `FinalDecision` にある。
これを測定側でやると、測定に課題定義と採点の語彙が必要になる。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import EvaluatorKind, GradingRun, Routing, TaskVersion
from aijudge_observation import Observation

from .store import FinalDecision, GoldenMark, QueueEntry


def project(
    entry: QueueEntry,
    task: TaskVersion,
    run: GradingRun,
    *,
    mark: GoldenMark | None = None,
    decision: FinalDecision | None = None,
) -> tuple[Observation, ...]:
    """1 提出ぶんの観測を作る。観点 1 つにつき 1 レコード。"""
    by_criterion = {score.criterion_id: score for score in run.criterion_scores}
    observed_at = datetime.now(UTC)

    observations: list[Observation] = []
    for criterion in task.criteria:
        score = by_criterion.get(criterion.id)
        levels = tuple(sorted(level.level for level in criterion.levels))
        if len(levels) < 2:
            # 段階が 1 つしかない観点は一致度を測れない。構成の誤りとして落とす。
            raise ValueError(
                f"criterion {criterion.code!r} declares fewer than two levels; "
                "agreement cannot be measured"
            )

        observations.append(
            Observation(
                subject_profile=entry.subject_profile,
                task_name=entry.task_name,
                submission=entry.submission,
                criterion_code=criterion.code,
                levels=levels,
                machine_level=None if score is None else score.level,
                machine_confidence=None if score is None else score.confidence,
                # 決定的評価が確定させた観点は AI の精度測定から外す。
                conclusive=score is not None
                and (score.conclusive or score.kind is EvaluatorKind.DETERMINISTIC),
                unscored=criterion.id in run.unscored_criteria,
                grading_run_id=run.id,
                human_level=None if mark is None else mark.marks.get(criterion.code),
                blind=bool(mark is not None and mark.blind),
                marker=None if mark is None else mark.marker,
                auto_confirmed=run.routing is Routing.AUTO,
                changed_after_seeing_ai=(
                    None if decision is None else decision.changed_after_seeing_ai
                ),
                observed_at=observed_at,
            )
        )
    return tuple(observations)
