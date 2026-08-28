"""成績の確定。

**確定は「教員が見た」とは別の事実である。** ここを 1 つの記録に混ぜると、
誰も読んでいない提出に「教員が AI の判定に同意した」という記録が残る。
受講 91 名 × 十数課題では大半が自動確定になるので、その記録で一致度を
測れば実力より高い数字が出る（ADR 0005 が禁じている形）。

    HumanReview   教員が 1 件を読んで下した判断。**κ の証拠はこれだけ。**
    Finalization  成績が確定したという事実。出所（source）を必ず持つ。

個別レビューからの確定では両方が生まれ、`Finalization.review_id` が
その `HumanReview` を指す。一括確定と自動確定では `Finalization` だけが
生まれ、測定側の見え方は「まだ教員が見ていない」のままになる。これは
実態のとおりで、測定を甘くしないために必要な性質である。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .grading import MIN_JUSTIFICATION_LENGTH, GradingRun, ReviewRequest, Routing
from .ids import FinalizationId, GradingRunId, HumanReviewId, UserId


class FinalizationSource(StrEnum):
    """成績が確定した経緯。

    学習者にそのまま見せる値でもある。「教員が確認した成績」と
    「締切後に自動で確定した成績」を同じ顔で出すのは嘘になる。
    """

    # 教員が 1 件を開いて確定した。HumanReview を伴う。
    INSTRUCTOR_REVIEW = "instructor_review"
    # 教員が課題単位で残りをまとめて確定した。個々は読んでいない。
    INSTRUCTOR_BULK = "instructor_bulk"
    # 締切から所定の時間が経過したので確定した。人は関与していない。
    DEADLINE_ELAPSED = "deadline_elapsed"

    @property
    def reviewed_individually(self) -> bool:
        """教員がその 1 件を実際に読んだか。"""
        return self is FinalizationSource.INSTRUCTOR_REVIEW


class Finalization(BaseModel):
    """成績が確定したという事実。1 採点につき 1 つ、追記のみ（P8）。

    やり直しは再採点から。二度確定できると成績が二つ存在する。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: FinalizationId
    grading_run_id: GradingRunId
    source: FinalizationSource
    # 確定させた人。自動確定では None。
    actor_id: UserId | None = None
    # 個別レビューからの確定なら、その HumanReview。
    review_id: HumanReviewId | None = None
    # 根拠説明。学習者に表示する。自動確定では規定の文面が入る。
    justification: str = Field(min_length=MIN_JUSTIFICATION_LENGTH)
    finalized_at: datetime

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        if self.source is FinalizationSource.INSTRUCTOR_REVIEW:
            if self.review_id is None:
                raise ValueError("an instructor_review finalization must name its HumanReview")
            if self.actor_id is None:
                raise ValueError("an instructor_review finalization must name its actor")
        if self.source is FinalizationSource.INSTRUCTOR_BULK:
            if self.actor_id is None:
                raise ValueError("an instructor_bulk finalization must name its actor")
            if self.review_id is not None:
                raise ValueError("an instructor_bulk finalization reviews nothing individually")
        # 人が関与していないことを型で示す。誰かの ID を借りて書くと、
        # あとから「その教員が確定した」と読めてしまう。
        if self.source is FinalizationSource.DEADLINE_ELAPSED and (
            self.actor_id is not None or self.review_id is not None
        ):
            raise ValueError("a deadline_elapsed finalization has no actor and no review")
        return self

    @property
    def reviewed_individually(self) -> bool:
        return self.source.reviewed_individually


# 自動確定の根拠説明。学習者にはこの文面がそのまま出る。
DEADLINE_JUSTIFICATION = (
    "締切から所定の時間が経過したため、AI の判定のまま確定しました。"
    "内容に疑問があるときは担当教員に申し出てください。"
)


class GradeWindow(StrEnum):
    """成績が確定するまでの段階。**保存しない。導出する。**

    締切と猶予から計算できるものを記録に持つと、締切を直したときに古い値が
    残る（`Task.due_at` は学期中に動く）。

        採点完了 ──→ OPEN         AI の判定。確定の予定はまだ告げられない
           締切 ──→ PROVISIONAL  仮確定。「いつ確定するか」を学習者に示す
        締切+n ──→ ELAPSED       確定してよい時刻を過ぎた

    **PROVISIONAL がこの設計の要点。** 締切と同時に「この点数は
    MM/DD HH:MM に確定します」と告げることで、学習者は異議をいつまでに
    出せばよいかが分かる。告げずに静かに確定させると、確定したこと自体が
    事後にしか分からない（ADR 0009 が避けたかった一方的な通告と同じ形）。
    """

    OPEN = "open"
    PROVISIONAL = "provisional"
    ELAPSED = "elapsed"


def deadline_for(due_at: datetime | None, after_hours: float | None) -> datetime | None:
    """自動確定の時刻。締切か設定のどちらかが無ければ自動確定しない。"""
    if due_at is None or after_hours is None:
        return None
    return due_at + timedelta(hours=after_hours)


def grade_window(
    due_at: datetime | None, after_hours: float | None, now: datetime
) -> GradeWindow:
    """いまどの段階か。

    自動確定を設定していないコースでは常に OPEN。確定の予定が無いのに
    「MM/DD に確定します」とは言えないし、期限を示していない以上、
    異議の受付を締め切ることもできない。
    """
    settles_at = deadline_for(due_at, after_hours)
    if settles_at is None or due_at is None:
        return GradeWindow.OPEN
    if now < due_at:
        return GradeWindow.OPEN
    return GradeWindow.PROVISIONAL if now < settles_at else GradeWindow.ELAPSED


def blocks_finalization(request: ReviewRequest | None) -> bool:
    """未対応の異議申立があるか。

    **依頼が出ているものを自動でも一括でも確定させない。** 学習者は
    「人に見てほしい」と言っている。そこへ機械が確定を書き込むと、
    異議申立の導線そのものが無意味になる（ADR 0009）。
    """
    return request is not None and not request.resolved


def auto_finalizable(run: GradingRun, request: ReviewRequest | None) -> bool:
    """締切経過による自動確定の対象か。

    人が関与しないので、条件は個別確定より厳しい。

    - `review_required` は対象外。レビュー方針が「人が見るべき」と判定した
      ものを、人を通さずに成績にしない（設計原則 P5）
    - 未採点の観点があるものも対象外。誰も見ていない観点が成績に入る
    - 未対応の異議申立があるものも対象外
    """
    if blocks_finalization(request):
        return False
    if run.is_provisional:
        return False
    return run.routing is Routing.AUTO


def bulk_finalizable(run: GradingRun, request: ReviewRequest | None) -> bool:
    """教員による一括確定の対象か。

    `review_required` も含める。教員が根拠説明を書いて明示的に責任を取る
    操作なので、自動確定と同じ制限は課さない。未対応の異議申立だけは
    外す ── そこは教員が 1 件ずつ読むべきものとして残す。
    """
    return not blocks_finalization(request)
