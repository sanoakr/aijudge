"""学習者に何を見せるか。

**AI の判定は採点直後に見せる。** 教員の確認を待たせない。

以前は待たせていた（設計原則 P5「教員が最終権限を持つ」を、そのまま
「教員が見るまで示さない」と読んでいた）。だが P5 が要求するのは
**最終権限が教員にあること**であって、途中経過を伏せることではない。
待たせると次のことが起きる。

- 学習者は締切前に自分の到達点を知れない。速く返せることが AI 採点の
  価値の中心なのに、その価値が教員の作業速度で消える
- 教員は全件を見るまで誰にも結果を返せない。受講 91 名で現実的でない

代わりに**異議申し立ての導線**を置く（設計方針 §9.4 が求めているもの）。
学習者は結果画面から再確認を依頼でき、そのとき**根拠説明を必須**にする。

    採点 → AI の判定を提示 → （学習者が疑えば）依頼 → 教員が確定

確定した成績は「教員が確認済み」として区別して見せる。区別しないと、
AI の判定と教員の判定が同じ重みに見える。

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
    # AI が判定した観点か。決定的評価（テスト実行）と区別して見せる。
    by_ai: bool = False

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
    # 総合点。AI の判定に基づく暫定値でも見せる（`confirmed` で区別する）。
    score_ratio: float | None
    confirmed: bool
    feedback: str | None = None
    # 教員の根拠説明（確定済みのとき）。
    review_comment: str | None = None
    # 再確認の依頼を出せるか。出せないなら理由。
    can_request_review: bool = False
    request_reason: str | None = None
    requested: bool = False

    @property
    def has_pending(self) -> bool:
        return any(view.pending for view in self.criteria)

    @property
    def provisional(self) -> bool:
        """まだ教員が確認していない点数か。"""
        return not self.confirmed


def build_result_view(
    run: GradingRun,
    task_version: TaskVersion,
    review: HumanReview | None,
    *,
    request: object | None = None,
) -> ResultView:
    """採点結果を学習者向けの表示に畳む。

    `request` はこの採点に対する再確認の依頼（`ReviewRequest`）。既に出して
    いれば二重に出させない。
    """
    by_criterion: dict[str, CriterionScore] = {
        str(score.criterion_id): score for score in run.criterion_scores
    }
    confirmed = review is not None

    views: list[CriterionView] = []
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
                # AI の判定か、決定的評価か。学習者が区別できるようにする。
                by_ai=score.kind is EvaluatorKind.AI,
            )
        )

    unscored = any(view.pending for view in views)
    can_request = not confirmed and request is None and not unscored
    reason: str | None = None
    if confirmed:
        reason = "担当教員が確認した成績です。"
    elif request is not None:
        reason = "再確認を依頼済みです。担当教員の対応をお待ちください。"
    elif unscored:
        reason = "採点できなかった観点があります。担当教員が確認します。"

    return ResultView(
        criteria=tuple(views),
        score_ratio=_confirmed_score(run, task_version, review),
        confirmed=confirmed,
        feedback=run.feedback,
        review_comment=None if review is None else review.comment,
        can_request_review=can_request,
        request_reason=reason,
        requested=request is not None,
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
