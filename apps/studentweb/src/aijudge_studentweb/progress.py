"""課題ごとの到達状況 ── 何回出して、いま何点で、どういう状態か。

一覧に出す値をここで作る。`visibility.py` が「1 件の提出をどう見せるか」を
決めるのに対し、ここは**その積み上げ**を決める。

一覧に出さないと何が起きるか。学習者は課題を 1 つずつ開かないと、自分が
その課題に何回出したのか、いま何点が付いているのかを知れない。回数と点は
「次に何をするか」を決める材料そのもので、それを見るのに 30 回クリックさせる
一覧は一覧として働いていない。

**点は 1 件ぶんの表示と同じ規則で作る。** 同じ提出が、一覧では点が出て
個別画面では「保留」になる（あるいはその逆）ことが無いように、両方とも
`build_result_view` を通す。ここで独自に合計を計算すると、保留の規則
（採点できなかった観点があるあいだ総合点を出さない）が一覧から漏れる。

**採点は最大値を採る。** 提出のたびに直すのが学習の形なので、最後の提出が
最高とは限らない（試しに壊してみた提出が最後になることがある）。同点なら
新しい方を採用として示す ── 点が同じなので値は変わらず、学習者にとっては
「いま出しているもの」が採られている方が読みやすい。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from aijudge_core import (
    Course,
    FinalizationSource,
    GradingRun,
    Submission,
    Task,
    TaskVersion,
    grace_minutes,
)
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import GradingRunRepository, ReviewRepository, SubmissionRepository

from .visibility import ResultView, build_result_view


@dataclass(frozen=True)
class AttemptSummary:
    """提出 1 件を一覧の 1 行に畳んだもの。"""

    submission: Submission
    run: GradingRun | None
    view: ResultView | None
    # この課題で採用される提出か（＝最高得点）。
    adopted: bool = False

    @property
    def graded(self) -> bool:
        return self.view is not None

    @property
    def score_ratio(self) -> float | None:
        """学習者に見せてよい総合点。保留中と未採点は None。"""
        return None if self.view is None else self.view.score_ratio

    @property
    def status_label(self) -> str:
        """状態を 1 語で表す。**確定の出所まで区別する**（ADR 0010）。

        「確認済み」でまとめると、誰も読んでいない自動確定が
        「教員が確認しました」として一覧に並ぶ。
        """
        view = self.view
        if view is None:
            return "採点中"
        if view.confirmed:
            if view.finalized_by is FinalizationSource.INSTRUCTOR_REVIEW:
                return "確定（教員が確認）"
            if view.finalized_by is FinalizationSource.INSTRUCTOR_BULK:
                return "確定（一括）"
            return "確定（自動）"
        if view.requested:
            return "確認依頼中"
        if view.score_withheld:
            return "保留"
        if view.provisional_pending:
            return "仮確定"
        return "確定前（AI の判定）"

    @property
    def status_tone(self) -> str:
        """ピルの見た目。確定＝緑、手当てが要るもの＝朱、途中＝無色。"""
        view = self.view
        if view is None:
            return ""
        if view.confirmed:
            return "ok"
        if view.requested or view.score_withheld:
            return "no"
        return ""

    @property
    def status_detail(self) -> str | None:
        """状態だけでは足りないときの一言。無ければ None。"""
        view = self.view
        if view is None:
            return None
        if view.score_withheld:
            return "採点できなかった観点があります"
        if not view.confirmed and view.settles_at is not None:
            return f"{view.settles_at.strftime('%m/%d %H:%M')} に確定"
        return None


@dataclass(frozen=True)
class TaskProgress:
    """1 つの課題に対する、その学習者のこれまで。"""

    attempts: tuple[AttemptSummary, ...]

    @property
    def count(self) -> int:
        return len(self.attempts)

    @property
    def adopted(self) -> AttemptSummary | None:
        """採用される提出（最高得点）。点の出ている提出が無ければ None。"""
        for attempt in self.attempts:
            if attempt.adopted:
                return attempt
        return None

    @property
    def best_ratio(self) -> float | None:
        adopted = self.adopted
        return None if adopted is None else adopted.score_ratio

    @property
    def best_confirmed(self) -> bool:
        """採用される点が確定済みか。暫定なら一覧でもそう示す。"""
        adopted = self.adopted
        return adopted is not None and adopted.view is not None and adopted.view.confirmed

    @property
    def grading(self) -> bool:
        """まだ採点が届いていない提出があるか。"""
        return any(attempt.view is None for attempt in self.attempts)

    @property
    def withheld(self) -> bool:
        """点を保留している提出があるか（採点できなかった観点がある）。"""
        return any(
            attempt.view is not None and attempt.view.score_withheld for attempt in self.attempts
        )


EMPTY = TaskProgress(attempts=())


class Reads(Protocol):
    """`load_progress` が読む先。

    `UnitOfWork` 全体ではなく**読む 3 つだけ**を要求する。一覧を作るのに
    キューや outbox は要らず、要求しなければ実装を差し替えるときの制約も
    それだけ小さくなる。
    """

    @property
    def submissions(self) -> SubmissionRepository: ...

    @property
    def runs(self) -> GradingRunRepository: ...

    @property
    def reviews(self) -> ReviewRepository: ...


def load_progress(
    uow: Reads,
    *,
    tenant_id: TenantId,
    learner_id: UserId,
    course: Course,
    rows: tuple[tuple[Task, TaskVersion], ...],
    now: datetime | None = None,
) -> dict[TaskVersionId, TaskProgress]:
    """課題一覧ぶんの到達状況を**まとめて**読む。

    課題ごとに問い合わせると、課題数 × 提出回数 × 4 のクエリになる
    （一覧を開くたびに）。提出・採点・人間側の記録をそれぞれ 1 回で引く。
    """
    wanted = {version.id: (task, version) for task, version in rows}
    if not wanted:
        return {}

    submissions = [
        submission
        for submission in uow.submissions.list_for_learner(tenant_id, learner_id)
        if submission.task_version_id in wanted
    ]
    runs = uow.runs.latest_for_many([submission.id for submission in submissions])
    decisions = uow.reviews.decisions_for_runs([run.id for run in runs.values()])

    by_version: dict[TaskVersionId, list[AttemptSummary]] = {}
    moment = now or datetime.now(UTC)
    for submission in submissions:
        task, version = wanted[submission.task_version_id]
        run = runs.get(submission.id)
        decision = None if run is None else decisions.get(run.id)
        view = (
            None
            if run is None
            else build_result_view(
                run,
                version,
                None if decision is None else decision.review,
                request=None if decision is None else decision.request,
                finalization=None if decision is None else decision.finalization,
                due_at=task.due_at,
                auto_finalize_after_minutes=grace_minutes(
                    task.auto_finalize_after_minutes, course.auto_finalize_after_minutes
                ),
                now=moment,
            )
        )
        by_version.setdefault(submission.task_version_id, []).append(
            AttemptSummary(submission=submission, run=run, view=view)
        )

    return {
        version_id: TaskProgress(attempts=_mark_adopted(attempts))
        for version_id, attempts in by_version.items()
    }


def _mark_adopted(attempts: list[AttemptSummary]) -> tuple[AttemptSummary, ...]:
    """最高得点の提出に印を付ける。同点なら後の提出を採る（モジュール冒頭）。

    `attempts` は古い順。点の出ていない提出（採点中・保留）は候補にしない ──
    保留中の提出を採用として示すと、そこに点が付いていないことが
    「0 点が採用された」に見える。
    """
    best = -1.0
    chosen: int | None = None
    for index, attempt in enumerate(attempts):
        ratio = attempt.score_ratio
        if ratio is not None and ratio >= best:
            best = ratio
            chosen = index
    if chosen is None:
        return tuple(attempts)
    marked = list(attempts)
    marked[chosen] = AttemptSummary(
        submission=marked[chosen].submission,
        run=marked[chosen].run,
        view=marked[chosen].view,
        adopted=True,
    )
    return tuple(marked)
