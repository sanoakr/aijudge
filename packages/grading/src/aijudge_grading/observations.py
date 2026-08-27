"""採点結果を観点単位の記録に投影する。

**これは測定ではない。** 採点した側が、自分の結果を観点 1 つにつき 1 行の
読み取り用の形（`Observation`）で公開するだけの純関数である。測定側は
この記録だけを読み、採点の語彙（`GradingRun` や `TaskVersion`）を知らない
（ADR 0007）。

投影をここに置く理由は、必要な材料が揃っている場所がここだからである。
観点コードと段階の集合は課題定義にあり、判定は採点結果にある。これを
測定側でやると、測定に課題定義と採点結果の読み方が必要になる。

段階の集合（`levels`）を各行に焼き込むのが要点。測定側で課題定義から
引き直す設計にすると、引けなかったときに既定値へ黙って落ち、QWK の
重み行列が狂って誤った値が出る。
"""

from __future__ import annotations

from datetime import datetime

from aijudge_core import EvaluatorKind, GradingRun, Routing, TaskVersion
from aijudge_observation import Observation


def project_observations(
    run: GradingRun,
    task_version: TaskVersion,
    *,
    subject_profile: str,
    task_name: str,
    submission: str,
    observed_at: datetime,
    human_levels: dict[str, int] | None = None,
    blind: bool = False,
    marker: str | None = None,
    machine_corrected: bool | None = None,
) -> tuple[Observation, ...]:
    """1 採点ぶんの観測を作る。観点 1 つにつき 1 行。

    `human_levels` は教員が AI を見る前に付けた段階。抽出対象外の提出では
    空で、そのとき一致度の標本には入らない（`usable_for_agreement` が偽）。
    """
    by_criterion = {score.criterion_id: score for score in run.criterion_scores}
    marks = human_levels or {}

    observations: list[Observation] = []
    for criterion in task_version.criteria:
        score = by_criterion.get(criterion.id)
        levels = tuple(sorted(level.level for level in criterion.levels))
        if len(levels) < 2:
            # 段階が 1 つしかない観点では一致度が定義できない。
            # コアの検証が通してしまう構成なので、ここで落とす。
            raise ValueError(
                f"criterion {criterion.code!r} declares fewer than two levels; "
                "agreement cannot be measured"
            )

        observations.append(
            Observation(
                subject_profile=subject_profile,
                task_name=task_name,
                submission=submission,
                criterion_code=criterion.code,
                levels=levels,
                machine_level=None if score is None else score.level,
                machine_confidence=None if score is None else score.confidence,
                # 決定的評価が確定させた観点は AI の精度測定から外す。
                conclusive=score is not None
                and (score.conclusive or score.kind is EvaluatorKind.DETERMINISTIC),
                unscored=criterion.id in run.unscored_criteria,
                grading_run_id=str(run.id),
                human_level=marks.get(criterion.code),
                blind=bool(marks and blind),
                marker=marker if marks else None,
                auto_confirmed=run.routing is Routing.AUTO,
                machine_corrected=machine_corrected,
                observed_at=observed_at,
            )
        )
    return tuple(observations)
