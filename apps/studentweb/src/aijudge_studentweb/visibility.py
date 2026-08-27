"""学習者に何を見せるか。

設計原則 P5（教員が最終権限を持つ）を画面の規則に落とす。

    決定的評価の結果   … すぐ見せる
    AI の判定          … 教員が確定させてから見せる
    根拠のハイライト    … 確定後
    総合点             … 確定後（暫定値を成績と誤解させない）

決定的評価をすぐ見せるのは、それが Sharif Judge から引き継ぐ価値の中心
だから（テストが通ったかは即座に分かるべき）。AI の判定を確定前に見せない
のは、教員が覆す前提の値を学習者が成績として受け取ってしまうため。異議
申し立ての導線も、確定した成績に対してでなければ意味がない。

**この判断は UI ではなくここに置く。** テンプレートの `{% if %}` に散らすと、
画面を 1 つ足したときに漏れる。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingRun,
    HumanReview,
    RubricCriterion,
    TaskVersion,
)


@dataclass(frozen=True)
class CriterionView:
    """学習者に見せる観点 1 つ。"""

    criterion: RubricCriterion
    level: int | None
    rationale: str | None
    evidence_lines: tuple[int, ...]
    # 採点されていない、または確定を待っている。
    pending: bool
    # 教員が AI の判定を変えた。
    adjusted: bool = False

    @property
    def label(self) -> str:
        if self.pending:
            return "確認中"
        if self.level is None:
            return "採点できませんでした"
        return self.criterion.level_for(self.level).label


@dataclass(frozen=True)
class ResultView:
    """1 採点ぶんの表示内容。"""

    criteria: tuple[CriterionView, ...]
    # 確定した総合点。未確定なら None（暫定値を成績として見せない）。
    score_ratio: float | None
    confirmed: bool
    awaiting_ai: bool
    feedback: str | None = None

    @property
    def has_pending(self) -> bool:
        return any(view.pending for view in self.criteria)


def build_result_view(
    run: GradingRun,
    task_version: TaskVersion,
    review: HumanReview | None,
) -> ResultView:
    """採点結果を学習者向けの表示に畳む。"""
    by_criterion: dict[str, CriterionScore] = {
        str(score.criterion_id): score for score in run.criterion_scores
    }
    confirmed = review is not None

    views: list[CriterionView] = []
    awaiting_ai = False
    for criterion in task_version.criteria:
        score = by_criterion.get(str(criterion.id))
        if score is None:
            # 評価器が落ちた観点。暫定であることを隠さない。
            views.append(
                CriterionView(
                    criterion=criterion,
                    level=None,
                    rationale=None,
                    evidence_lines=(),
                    pending=True,
                )
            )
            continue

        deterministic = score.kind is EvaluatorKind.DETERMINISTIC
        if not deterministic and not confirmed:
            # AI の判定は教員が確定させてから見せる。
            awaiting_ai = True
            views.append(
                CriterionView(
                    criterion=criterion,
                    level=None,
                    rationale=None,
                    evidence_lines=(),
                    pending=True,
                )
            )
            continue

        level = score.level
        adjusted = False
        if review is not None:
            level = review.level_for(criterion.id, score.level)
            adjusted = criterion.id in review.adjusted_levels

        views.append(
            CriterionView(
                criterion=criterion,
                level=level,
                rationale=score.rationale,
                evidence_lines=_evidence_lines(score),
                pending=False,
                adjusted=adjusted,
            )
        )

    return ResultView(
        criteria=tuple(views),
        # 確定していない総合点は見せない。暫定値を成績と誤解させないため。
        score_ratio=_confirmed_score(run, task_version, review) if confirmed else None,
        confirmed=confirmed,
        awaiting_ai=awaiting_ai,
        feedback=run.feedback if confirmed else None,
    )


def _evidence_lines(score: CriterionScore) -> tuple[int, ...]:
    lines: set[int] = set()
    for evidence in score.evidence:
        span = evidence.span
        if span.kind == "line":
            lines.update(range(span.start_line, span.end_line + 1))
    return tuple(sorted(lines))


def _confirmed_score(
    run: GradingRun, task_version: TaskVersion, review: HumanReview | None
) -> float:
    """確定した段階から総合点を出し直す。

    教員が段階を変えたなら、総合点もそれに従う。`GradingRun.score_ratio` は
    AI の判定に基づく値なので、そのまま成績として見せると教員の修正が
    反映されない。
    """
    if review is None or review.agreed:
        return run.score_ratio

    total = 0.0
    for score in run.criterion_scores:
        criterion = next((c for c in task_version.criteria if c.id == score.criterion_id), None)
        if criterion is None:  # pragma: no cover - 課題版が一致しない構成
            continue
        level = review.level_for(score.criterion_id, score.level)
        total += criterion.level_for(level).score_ratio * score.weight
    return round(min(1.0, max(0.0, total)), 10)
