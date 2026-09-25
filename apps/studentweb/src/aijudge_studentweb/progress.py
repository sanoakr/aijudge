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

**提出は課題ごとに数え、版をまたぐ**（2026-09-24）。課題を訂正すると版が上がり
（`course apply --revise`・P8）、前の版への提出はその版を指したまま残る。
以前はいまの版への提出だけを並べていたので、版が上がった瞬間に学習者の提出が
画面から消えて「未提出」に戻り、出し直す学生が出た（network ex1、9/23）。
教員の画面と成績の採用（`aijudge_reviewconsole.submissions.adopted_ids`）は
学習者・課題ごとに版をまたいで決めており、学習者の画面だけが違っていた。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from aijudge_authoring import TaskRepository
from aijudge_core import (
    Course,
    FinalizationSource,
    GradingRun,
    Submission,
    Task,
    TaskVersion,
    grace_minutes,
    max_scores_by_version,
)
from aijudge_core.ids import TaskId, TaskVersionId, TenantId, UserId
from aijudge_submission import GradingRunRepository, ReviewRepository, SubmissionRepository
from aijudge_webui import local_filter

from .visibility import ResultView, build_result_view


@dataclass(frozen=True)
class AttemptSummary:
    """提出 1 件を一覧の 1 行に畳んだもの。"""

    submission: Submission
    run: GradingRun | None
    view: ResultView | None
    # この課題で採用される提出か（＝最高得点）。
    adopted: bool = False
    # 課題の中で何回目か（版をまたいで古い順に 1 から）。`Submission.attempt` は
    # **版ごと**に数えるので、版が上がると 1 に戻る ── 並べると「1 回目」が
    # 2 つ出る。
    number: int = 0
    # いま出ている版より前の版への提出か。問題文や観点が今と違いうるので、
    # 画面でそう断る（点はその版の観点で付いたもの）。
    earlier_version: bool = False
    # この提出を数える配点（`effective_max_score`・2026-09-25）。**提出が指す版**の
    # 値で、いまの版の値ではない ── 教員が配点を下げても、取った点は下がらない。
    max_points: float = 100.0
    # この課題に配点が入っているか（どれかの版が `points_declared`）。**入っていなければ
    # 割合だけを見せる**（従来どおり）── 既定の 100 を点数として出すと、配点を決めて
    # いない課題に「80 / 100 点」が並ぶ。
    pointed: bool = False

    @property
    def points(self) -> float | None:
        """得点（割合 × 配点）。割合を見せられないとき（保留・未採点）は None。"""
        ratio = self.score_ratio
        return None if ratio is None else ratio * self.max_points

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
            return f"{local_filter(view.settles_at, '%m/%d %H:%M')} に確定"
        return None


@dataclass(frozen=True)
class TaskProgress:
    """1 つの課題に対する、その学習者のこれまで。"""

    attempts: tuple[AttemptSummary, ...]
    # いま出ている版の配点。課題一覧に出す（提出が無くても要る）。
    max_points: float = 100.0
    # 配点が入っている課題か（`AttemptSummary.pointed`）。偽なら割合だけを見せる。
    pointed: bool = False

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
    def best_points(self) -> float | None:
        """採用される提出の得点。採用が無ければ None。"""
        adopted = self.adopted
        return None if adopted is None else adopted.points

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

    @property
    def tasks(self) -> TaskRepository: ...


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
    # 課題 → いま出ている版。前の版への提出もここへ寄せる（モジュール冒頭）。
    current: dict[TaskId, tuple[Task, TaskVersion]] = {
        task.id: (task, version) for task, version in rows
    }

    mine = uow.submissions.list_for_learner(tenant_id, learner_id)
    # この一覧の課題の版を**まとめて 1 回で**引く。前の版への提出を寄せるのにも、
    # 配点（`effective_max_score` は版の履歴で決まる）にも要る。
    history = uow.tasks.versions_for_tasks(current)
    earlier = {version.id: version for version in history if version.id not in wanted}
    max_points = max_scores_by_version([*history, *(version for _, version in rows)])
    pointed = {version.task_id for version in history if version.points_declared}
    pointed |= {version.task_id for _, version in rows if version.points_declared}

    submissions = [s for s in mine if s.task_version_id in wanted or s.task_version_id in earlier]
    runs = uow.runs.latest_for_many([submission.id for submission in submissions])
    decisions = uow.reviews.decisions_for_runs([run.id for run in runs.values()])

    by_version: dict[TaskVersionId, list[AttemptSummary]] = {}
    moment = now or datetime.now(UTC)
    for submission in submissions:
        if submission.task_version_id in wanted:
            task, own = wanted[submission.task_version_id]
            shown = own
        else:
            own = earlier[submission.task_version_id]
            task, shown = current[own.task_id]
        run = runs.get(submission.id)
        decision = None if run is None else decisions.get(run.id)
        view = (
            None
            if run is None
            else build_result_view(
                run,
                # **提出が指す版**で見せる。点はその版の観点で付いている ──
                # いまの版の観点で読むと、観点の数や重みが違えば保留の判定まで狂う。
                own,
                None if decision is None else decision.review,
                request=None if decision is None else decision.request,
                finalization=None if decision is None else decision.finalization,
                auto_finalize_after_minutes=grace_minutes(
                    task.auto_finalize_after_minutes, course.auto_finalize_after_minutes
                ),
                now=moment,
            )
        )
        by_version.setdefault(shown.id, []).append(
            AttemptSummary(
                submission=submission,
                run=run,
                view=view,
                earlier_version=own.version < shown.version,
                max_points=max_points.get(own.id, own.max_score),
                pointed=own.task_id in pointed,
            )
        )

    # **提出の無い課題も返す**（配点を課題一覧に出すため）。数は 0 のまま。
    return {
        version.id: TaskProgress(
            attempts=_mark_adopted(_numbered(by_version.get(version.id, []))),
            max_points=max_points.get(version.id, version.max_score),
            pointed=version.task_id in pointed,
        )
        for _task, version in rows
    }


def _numbered(attempts: list[AttemptSummary]) -> list[AttemptSummary]:
    """古い順に並べ、課題の中での通し番号を振る。

    版をまたぐので `Submission.attempt` では並べられない（版ごとに 1 から）。
    比べるのは採用の規則と同じ (提出時刻, 回数) ── 教員側の
    `adopted_ids` と同点の扱いを揃える。
    """
    ordered = sorted(
        attempts,
        key=lambda a: (
            a.submission.submitted_at or a.submission.created_at,
            a.submission.attempt,
        ),
    )
    return [replace(attempt, number=index) for index, attempt in enumerate(ordered, start=1)]


def _mark_adopted(attempts: list[AttemptSummary]) -> tuple[AttemptSummary, ...]:
    """最高得点の提出に印を付ける。同点なら後の提出を採る（モジュール冒頭）。

    **点数で比べる**（割合 × その版の配点・2026-09-25）。教員側の `adopted_ids` と
    同じ規則 ── 版をまたぐと、割合と点数の大小が食い違う。

    `attempts` は古い順。点の出ていない提出（採点中・保留）は候補にしない ──
    保留中の提出を採用として示すと、そこに点が付いていないことが
    「0 点が採用された」に見える。
    """
    best = -1.0
    chosen: int | None = None
    for index, attempt in enumerate(attempts):
        points = attempt.points
        if points is not None and points >= best:
            best = points
            chosen = index
    if chosen is None:
        return tuple(attempts)
    marked = list(attempts)
    marked[chosen] = replace(marked[chosen], adopted=True)
    return tuple(marked)
