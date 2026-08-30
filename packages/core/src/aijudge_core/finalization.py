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
    # 採点から所定の時間が経過したので確定した。人は関与していない。
    #
    # **値は `deadline_elapsed` のまま。** 起点は締切から採点完了に変えたが、
    # 確定済みの記録がこの文字列で保存されている。値を変えると過去の成績が
    # 「経緯不明」になるので、名前だけ歴史的なものとして残す。
    AUTOMATIC = "deadline_elapsed"

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
        if self.source is FinalizationSource.AUTOMATIC and (
            self.actor_id is not None or self.review_id is not None
        ):
            raise ValueError("a deadline_elapsed finalization has no actor and no review")
        return self

    @property
    def reviewed_individually(self) -> bool:
        return self.source.reviewed_individually


# 自動確定の根拠説明。学習者にはこの文面がそのまま出る。
AUTOMATIC_JUSTIFICATION = (
    "採点から所定の時間が経過したため、AI の判定のまま確定しました。"
    "内容に疑問があるときは担当教員に申し出てください。"
    "課題の締切前であれば、直して出し直せます（良い方の点が採用されます）。"
)


class GradeWindow(StrEnum):
    """成績が確定するまでの段階。**保存しない。導出する。**

    計算できるものを記録に持つと、元の値を直したときに古い段階が残る。

        自動確定の設定なし ──→ OPEN         確定の予定が無い
        採点完了           ──→ PROVISIONAL  仮確定。「いつ確定するか」を示す
        採点完了+n         ──→ ELAPSED      確定してよい時刻を過ぎた

    **起点は締切ではなく採点完了である。** 締切を起点にすると、締切前に
    出した学習者は自分の点が確定するまで何日も待つことになり、その間は
    再提出の判断材料が「暫定」のままになる。採点は提出直後に終わるので、
    そこから n 分で閉じれば、**締切前に確定し、締切前に出し直せる**
    （出し直しは確定に妨げられない ── `Task.accepts_submissions_at` は
    確定を見ない。採用されるのは最高得点の提出）。

    **PROVISIONAL がこの設計の要点。** 「この点数は MM/DD HH:MM に確定
    します」と告げることで、学習者は異議をいつまでに出せばよいかが分かる。
    告げずに静かに確定させると、確定したこと自体が事後にしか分からない
    （ADR 0009 が避けたかった一方的な通告と同じ形）。
    """

    OPEN = "open"
    PROVISIONAL = "provisional"
    ELAPSED = "elapsed"


def settles_at(graded_at: datetime | None, after_minutes: int | None) -> datetime | None:
    """自動確定の時刻。**起点は採点が終わった時刻。**

    設定が無ければ自動確定しない（既定はそれで、教員が明示的に入れて
    初めて始まる）。

    猶予は**分**。時間単位だと「採点の 10 分後に確定」が表せず、演習中に
    出してその場で返す使い方ができない。
    """
    if graded_at is None or after_minutes is None:
        return None
    return graded_at + timedelta(minutes=after_minutes)


def grace_minutes(task_value: int | None, course_value: int | None) -> int | None:
    """実際に効く猶予。**問題セットの指定が科目の既定を上書きする。**

    問題セットに何も入れていなければ科目の値。科目にも無ければ自動確定
    しない（既定はそれで、教員が明示的に入れて初めて始まる）。
    """
    return course_value if task_value is None else task_value


def grade_window(
    graded_at: datetime | None, after_minutes: int | None, now: datetime
) -> GradeWindow:
    """いまどの段階か。**採点が終わった時刻を起点に見る。**

    自動確定を設定していないコースでは常に OPEN。確定の予定が無いのに
    「MM/DD に確定します」とは言えないし、期限を示していない以上、
    異議の受付を締め切ることもできない。
    """
    closes = settles_at(graded_at, after_minutes)
    if closes is None:
        return GradeWindow.OPEN
    return GradeWindow.PROVISIONAL if now < closes else GradeWindow.ELAPSED


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
    - 点が定まっていないもの（`is_provisional`）も対象外。評価器が落ちた
      観点や、人の採点を待っている観点が成績に入る
    - 未対応の異議申立があるものも対象外

    **集約のゲートで打ち切った観点は止めない。** 0% は確定した結果なので、
    人が見るべきものは何も残っていない（Issue #10）。`is_provisional` が
    その観点を暫定に数えないことでそうなっている。
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
