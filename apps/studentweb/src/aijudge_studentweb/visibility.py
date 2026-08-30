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

そして**期限を示す**。締切を過ぎた採点は「仮確定」として、いつ確定するかを
学習者に告げる（`GradeWindow`）。

    締切      仮確定。「MM/DD HH:MM に確定します」と示し、異議を受け付ける
    締切+n    確定。依頼はここで締め切る

期限を示さずに静かに確定させると、確定したこと自体が事後にしか分からない。
逆に期限を示した以上、そこで締め切ってよい ── 締め切らないと、教員の待ち行列は
学期末まで新しい依頼を受け続ける。確定後の申し出は画面の外（担当教員）に回す。

**採点できなかった観点があるときは、総合点を出さない。**

S6 が止まっていれば AI 評価器は飛ばされ、その観点は `unscored_criteria` に
残る（P2 の劣化動作）。このとき決定的評価の結果は**そのまま返す** ── テスト
実行の合否は確定した事実で、伏せる理由が無いし、学習者が次に直すものはそこから
分かる。

伏せるのは**全体の仮評価**である。残った観点だけを比例配分した合計は、
「読みやすさが未評価のまま 100%」のように、欠けている観点を知らない学習者に
到達度として読まれる。観点の一覧には「確認中」と出ているが、大きく出る数字の
方が先に目に入る。部分的な合計は、正しい数字より誤読される数字である。

教員には出す。欠けを埋めて確定させるのが教員の仕事で、決定的評価が何を返した
かはその判断材料になる。

確定した成績は区別して見せる。**確定の出所まで区別する。** 教員が読んで
確定したものと、締切後に機械が確定したものを同じ「確認済み」で出すのは
学習者に対する嘘になる（ADR 0010）。前者は人が根拠を書いており、後者は
まだ誰も読んでいない ── 学習者が異議を申し立てるかどうかの判断が変わる。

**この判断は UI ではなくここに置く。** テンプレートの `{% if %}` に散らすと、
画面を 1 つ足したときに漏れる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    Finalization,
    FinalizationSource,
    GradeWindow,
    GradingRun,
    HumanReview,
    LatePenalty,
    RubricCriterion,
    TaskVersion,
    final_score,
    grade_window,
    settles_at,
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
    # 総合点（遅延の減点を引いたあと）。AI の判定に基づく暫定値でも見せる
    # （`confirmed` で区別する）。ただし**採点できなかった観点があるあいだは
    # None**（保留）。
    score_ratio: float | None
    confirmed: bool
    # 遅延の減点。**評価点と分けて見せる**（P4）。合計だけを出すと、
    # 何点引かれたのかが学習者にも異議申立にも分からない。
    penalty: LatePenalty | None = None
    # 減点前の評価点。減点があるときだけ意味を持つ。
    evaluation_ratio: float | None = None
    # 教員が減点を免除したか。
    penalty_waived: bool = False
    feedback: str | None = None
    # 確定の出所。確定していなければ None。
    finalized_by: FinalizationSource | None = None
    # いまどの段階か（締切前 / 仮確定 / 期限経過）。
    window: GradeWindow = GradeWindow.OPEN
    # 確定する（した）予定時刻。自動確定を設定していないコースでは None。
    settles_at: datetime | None = None
    # 根拠説明（確定済みのとき）。教員が書いたものか、自動確定の定型文。
    review_comment: str | None = None
    # 再確認の依頼を出せるか。出せないなら理由。
    can_request_review: bool = False
    request_reason: str | None = None
    requested: bool = False

    @property
    def has_pending(self) -> bool:
        return any(view.pending for view in self.criteria)

    @property
    def penalized(self) -> bool:
        """遅延の減点が実際に効いているか。免除されていれば偽。"""
        return self.penalty is not None and not self.penalty_waived

    @property
    def score_withheld(self) -> bool:
        """総合点を保留しているか。

        観点ごとの結果は出ているのに合計だけが無い状態なので、**なぜ無いのかを
        必ず添える**（テンプレートの責務）。黙って空にすると、採点が壊れたのか
        点が 0 なのか学習者に区別できない。
        """
        return self.score_ratio is None and bool(self.criteria)

    @property
    def provisional(self) -> bool:
        """まだ確定していない点数か。"""
        return not self.confirmed

    @property
    def reviewed_individually(self) -> bool:
        """教員がこの提出を実際に読んだか。

        確定していても偽になりうる（一括確定・自動確定）。**この区別を
        表示から落とさない。** 落とすと、誰も読んでいない成績が「教員が
        確認しました」として学習者に出る。
        """
        return self.finalized_by is FinalizationSource.INSTRUCTOR_REVIEW

    @property
    def provisional_pending(self) -> bool:
        """仮確定か ── 締切を過ぎ、確定の予定時刻が示されていて、まだ確定していない。"""
        return not self.confirmed and self.window is GradeWindow.PROVISIONAL

    @property
    def confirmation_label(self) -> str:
        """確定の状態を 1 行で表す。テンプレートはこれを出す。"""
        if self.finalized_by is None:
            return "AI の判定（確定前）"
        if self.finalized_by is FinalizationSource.INSTRUCTOR_REVIEW:
            return "担当教員が確認した成績"
        if self.finalized_by is FinalizationSource.INSTRUCTOR_BULK:
            return "担当教員が確定した成績（個別の確認は経ていません）"
        return "締切後に自動で確定した成績（個別の確認は経ていません）"


def build_result_view(
    run: GradingRun,
    task_version: TaskVersion,
    review: HumanReview | None,
    *,
    request: object | None = None,
    finalization: Finalization | None = None,
    auto_finalize_after_minutes: int | None = None,
    now: datetime | None = None,
) -> ResultView:
    """採点結果を学習者向けの表示に畳む。

    `request` はこの採点に対する再確認の依頼（`ReviewRequest`）。既に出して
    いれば二重に出させない。

    仮確定の窓は**この採点が終わった時刻**（`run.created_at`）と
    `auto_finalize_after_minutes` から出す。**締切は起点にしない** ── 締切
    起点だと、締切前に出した学習者は自分の点が確定するまで何日も待つ。
    採点は提出直後に終わるので、そこから n 分で閉じれば締切前に確定し、
    締切前に出し直せる（出し直しは確定に妨げられない）。

    猶予が設定されていなければ確定の予定は無く、窓は開いたまま（期限を
    示していないので締め切れない）。

    `finalization` が成績の確定である。`review` はそのうち「教員が読んだ」
    場合にだけ在り、段階の修正を持つ（ADR 0010）。片方だけを見て確定を
    判断すると、一括確定・自動確定した成績が暫定のまま学習者に出続ける。

    **`review` は `finalization` より後から来ることがある。** 自動確定した
    成績に学習者が異議を申し立て、教員が読んで修正する経路がそれで、
    そのとき確定の記録は最初のもの（自動確定）のままである（追記のみ、P8）。
    学習者に見せる顔は教員が読んだ方を採る ── 後から人が読んだのなら、
    それがその成績について言える一番強いことだから。
    """
    by_criterion: dict[str, CriterionScore] = {
        str(score.criterion_id): score for score in run.criterion_scores
    }
    confirmed = finalization is not None or review is not None

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
    window = grade_window(run.created_at, auto_finalize_after_minutes, now or datetime.now(UTC))
    closes_at = settles_at(run.created_at, auto_finalize_after_minutes)
    # 教員が読んだ証拠は `HumanReview` の存在。確定の出所ではない
    # （自動確定のあとに教員が読むことがある）。
    reviewed = review is not None

    # **期限が来たら締め切る。** 締切と同時に「いつ確定するか」を示し、
    # n 時間の窓を与えてある。締め切らないと、教員の待ち行列は学期末まで
    # 新しい依頼を受け続ける。確定後の申し出は画面の外（担当教員）に回す。
    #
    # 締め切りは**時刻で**決め、確定の有無では決めない。自動確定は定期実行
    # なので期限と実際の確定の間に隙があり、そこで出された依頼は自動確定を
    # 恒久的に止める（誰も気づかないまま学期末まで残る）。
    elapsed = window is GradeWindow.ELAPSED
    can_request = not confirmed and not elapsed and request is None and not unscored

    reason: str | None = None
    if reviewed:
        reason = "担当教員が確認した成績です。"
    elif confirmed or elapsed:
        reason = "確定済みの成績です。申し出は担当教員に直接お願いします。"
    elif request is not None:
        reason = "再確認を依頼済みです。担当教員の対応をお待ちください。"
    elif unscored:
        reason = "採点できなかった観点があります。担当教員が確認します。"

    # 採点できなかった観点があるあいだは総合点を出さない（モジュール冒頭）。
    # 教員が確認して欠けを埋めれば出す。
    withhold = unscored and review is None
    score = final_score(run, task_version, review)

    return ResultView(
        criteria=tuple(views),
        score_ratio=None if withhold else score.final,
        penalty=run.penalty,
        evaluation_ratio=None if withhold else score.evaluation,
        penalty_waived=score.waived,
        confirmed=confirmed,
        feedback=run.feedback,
        finalized_by=_finalized_by(finalization, review),
        window=window,
        settles_at=closes_at,
        review_comment=_justification(finalization, review),
        can_request_review=can_request,
        request_reason=reason,
        requested=request is not None,
    )


def _finalized_by(
    finalization: Finalization | None, review: HumanReview | None
) -> FinalizationSource | None:
    """学習者に示す確定の出所。

    教員が読んでいれば、確定の記録が自動確定であってもそう示す。読んだ人が
    居るという事実の方が、いつ確定したかより学習者にとって重い。
    """
    if review is not None:
        return FinalizationSource.INSTRUCTOR_REVIEW
    return None if finalization is None else finalization.source


def _justification(finalization: Finalization | None, review: HumanReview | None) -> str | None:
    """根拠説明。教員が読んでいればその言葉を出す。"""
    if review is not None:
        return review.comment
    return None if finalization is None else finalization.justification


def _evidence_lines(score: CriterionScore) -> tuple[int, ...]:
    lines: set[int] = set()
    for evidence in score.evidence:
        span = evidence.span
        if span.kind == "line":
            lines.update(range(span.start_line, span.end_line + 1))
    return tuple(sorted(lines))
