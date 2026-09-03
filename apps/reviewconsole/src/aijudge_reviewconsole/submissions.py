"""提出の一覧と絞り込み。

**教員が「実際に何が出ているか」を見る場所。** 待ち行列（再確認の依頼）と
確定処理は、手を動かす必要があるものだけを出す ── そこに全提出を混ぜると、
どちらの画面も使えなくなる（ADR 0009）。一方で、授業の途中で「第 3 回の
p2 はどのくらい通っているか」を見たいことは常にあり、その入口がここである。

**絞り込みは URL に載せる。** 状態をサーバに持たない ── 教員は絞った結果を
そのまま学生や TA に渡すことがあり、リンクで渡せないと画面の説明から
始めることになる。

`adopted`（採用提出だけ）は、学習者に見えている成績と同じ見方をするための
ものである。同じ課題に何度も出すのが学習の形なので、全提出を平らに数えた
分布は「何度も試した人ほど低い点が多い」という形になり、到達度としては
読めない（`aijudge_studentweb.progress` が同じ規則を学習者側で使っている）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aijudge_core import (
    Course,
    Finalization,
    FinalizationSource,
    GradingRun,
    HumanReview,
    ReviewRequest,
    Role,
    Submission,
    Task,
    TaskVersion,
    final_score,
)

from .overview import unit_key

# 分布の階級。0-9, 10-19, …, 100 の 11 本。**100% は独立させる** ──
# 満点かどうかは教員が最初に見るところで、90 台に混ぜると読めない。
BUCKETS = 11


@dataclass(frozen=True)
class Row:
    """一覧の 1 行。"""

    submission: Submission
    run: GradingRun | None
    task: Task
    version: TaskVersion
    learner: object | None
    # **提出したときの役割**（#108）。いまの受講から引かない ── 学生が TA に
    # なった瞬間に、その人の過去の提出が絞り込みから消える（ADR 0013 の轍）。
    role: Role
    score: float | None
    finalized_by: FinalizationSource | None
    # **誰が閉じたか**（#102）。自動確定では空。出所（`finalized_by`）だけでは
    # 「誰の判断で成績が閉じたのか」に答えられない ── 記録はあった
    # （`Finalization.actor_id`）が、画面に出ていなかった。
    finalized_by_login: str
    contested: bool
    # この学習者のこの課題で、いちばん点の高い提出か（＝成績に採用される）。
    adopted: bool = False

    @property
    def is_trial(self) -> bool:
        """成績にも統計にも数えない提出か（#108）。"""
        return self.submission.is_trial

    @property
    def unit(self) -> str:
        return unit_key(self.task)

    @property
    def login(self) -> str:
        return getattr(self.learner, "login", "") or str(self.submission.learner_id)

    @property
    def state_label(self) -> str:
        if self.run is None:
            return "採点中"
        if self.finalized_by is FinalizationSource.INSTRUCTOR_REVIEW:
            return "確定（教員が確認）"
        if self.finalized_by is FinalizationSource.INSTRUCTOR_BULK:
            return "確定（一括）"
        if self.finalized_by is FinalizationSource.AUTOMATIC:
            return "確定（自動）"
        if self.contested:
            return "再確認の依頼あり"
        return "確定前"


@dataclass(frozen=True)
class Filters:
    """絞り込みの条件。**URL の問い合わせ文字列がそのまま入る。**"""

    unit: str = ""
    task: str = ""
    learner: str = ""
    role: str = ""
    state: str = ""
    adopted: bool = False

    def matches(self, row: Row) -> bool:
        if self.unit and row.unit != self.unit:
            return False
        if self.task and str(row.task.id) != self.task:
            return False
        # **前方一致。** 受講 91 名の学籍番号を選択肢に並べても選べない。
        if self.learner and not row.login.lower().startswith(self.learner.lower()):
            return False
        if self.role and row.role.value != self.role:
            return False
        if self.state and _state_key(row) != self.state:
            return False
        return not (self.adopted and not row.adopted)


STATE_LABELS: dict[str, str] = {
    "grading": "採点中",
    "open": "確定前",
    "contested": "再確認の依頼あり",
    "finalized": "確定済み",
}


def _state_key(row: Row) -> str:
    if row.run is None:
        return "grading"
    if row.finalized_by is not None:
        return "finalized"
    return "contested" if row.contested else "open"


@dataclass
class Distribution:
    """絞り込んだ結果の分布。**画面に出すのは数字ではなく形である。**

    平均だけを出すと、二山（できた人とまったく手が付かなかった人）が
    片方の数字に潰れる。実際の演習ではそれが最も見たい形なので、階級で
    持つ。
    """

    counts: list[int] = field(default_factory=lambda: [0] * BUCKETS)
    scored: int = 0
    total: int = 0

    @property
    def peak(self) -> int:
        return max(self.counts) if self.counts else 0

    @property
    def mean(self) -> float | None:
        if not self.scored:
            return None
        return sum(index * 10 * count for index, count in enumerate(self.counts)) / self.scored

    def label(self, index: int) -> str:
        return "100" if index == BUCKETS - 1 else f"{index * 10}"


def distribution_of(rows: list[Row]) -> Distribution:
    """得点の分布。**教員・TA 自身の試行は数えない**（#108）。

    動作確認で通した入力は到達度ではない。混ぜると「この課題は正答率が低い」
    が、実は教員が壊れた入力を試した結果、という形で現れる。
    """
    rows = [row for row in rows if not row.is_trial]
    result = Distribution(total=len(rows))
    for row in rows:
        if row.score is None:
            continue
        result.scored += 1
        percent = max(0.0, min(1.0, row.score)) * 100
        index = BUCKETS - 1 if percent >= 100 else int(percent // 10)
        result.counts[index] += 1
    return result


def load_rows(uow: object, course: Course) -> list[Row]:
    """このコースの全提出を、採点と人間側の記録まで揃えて読む。

    **1 件ずつ引かない。** 受講 91 名 × 課題十数件 × 再提出で数千件になり、
    提出ごとに 4 回問い合わせると一覧を開くたびにそれを踏む
    （`latest_for_many` / `decisions_for_runs` はそのためにある）。
    """
    submissions = uow.submissions.list_for_course(course.id)  # type: ignore[attr-defined]
    runs = uow.runs.latest_for_many([s.id for s in submissions])  # type: ignore[attr-defined]
    decisions = uow.reviews.decisions_for_runs([r.id for r in runs.values()])  # type: ignore[attr-defined]

    versions: dict[str, TaskVersion] = {}
    tasks: dict[str, Task] = {}
    for task in uow.tasks.list_for_course(course.id):  # type: ignore[attr-defined]
        tasks[str(task.id)] = task
    learners: dict[str, object] = {}
    # 確定者・レビュー者。受講者とは限らない（管理者が閉じることがある）ので
    # 受講の一覧からは引けない。**同じ人を何度も引かない。**
    actors: dict[str, str] = {}

    rows: list[Row] = []
    for submission in submissions:
        version = versions.get(str(submission.task_version_id))
        if version is None:
            version = uow.tasks.get_version(submission.task_version_id)  # type: ignore[attr-defined]
            if version is None:
                continue
            versions[str(version.id)] = version
        task = tasks.get(str(version.task_id))
        if task is None:
            continue
        learner_id = str(submission.learner_id)
        if learner_id not in learners:
            learners[learner_id] = uow.identity.get_user(submission.learner_id)  # type: ignore[attr-defined]

        run = runs.get(submission.id)
        decision = None if run is None else decisions.get(run.id)
        review: HumanReview | None = None if decision is None else decision.review
        request: ReviewRequest | None = None if decision is None else decision.request
        finalization: Finalization | None = None if decision is None else decision.finalization
        rows.append(
            Row(
                submission=submission,
                run=run,
                task=task,
                version=version,
                learner=learners[learner_id],
                role=submission.submitted_as,
                score=None if run is None else final_score(run, version, review).final,
                finalized_by=_finalized_by(finalization, review),
                finalized_by_login=_actor_login(finalization, review, uow, actors),
                contested=request is not None and not request.resolved,
            )
        )
    return _mark_adopted(rows)


def _actor_login(
    finalization: Finalization | None,
    review: HumanReview | None,
    uow: object,
    cache: dict[str, str],
) -> str:
    """成績を閉じた人の login。**読んだ人を優先する**（ADR 0010）。

    一括確定と自動確定は誰も読んでいない。前者には操作した教員が居るので
    その人を出し、後者は空にする ── 人が居ない確定に人の名前を出すと、
    「その教員が確認した」と読めてしまう。
    """
    actor_id = None
    if review is not None:
        actor_id = review.grader_id
    elif finalization is not None:
        actor_id = finalization.actor_id
    if actor_id is None:
        return ""
    key = str(actor_id)
    if key not in cache:
        user = uow.identity.get_user(actor_id)  # type: ignore[attr-defined]
        cache[key] = getattr(user, "login", "") or key
    return cache[key]


def _finalized_by(
    finalization: Finalization | None, review: HumanReview | None
) -> FinalizationSource | None:
    """確定の出所。**教員が読んでいればそう示す**（ADR 0010）。"""
    if review is not None:
        return FinalizationSource.INSTRUCTOR_REVIEW
    return None if finalization is None else finalization.source


def _mark_adopted(rows: list[Row]) -> list[Row]:
    """学習者・課題ごとに最高点の提出へ印を付ける。

    同点なら後の提出を採る（`aijudge_studentweb.progress` と同じ規則 ──
    学習者に見えている採用と教員が見る採用がずれてはいけない）。
    """
    best: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        if row.score is None:
            continue
        key = (str(row.submission.learner_id), str(row.task.id))
        current = best.get(key)
        if current is None or row.score >= (rows[current].score or -1.0):
            best[key] = index
    for index in best.values():
        row = rows[index]
        rows[index] = Row(
            submission=row.submission,
            run=row.run,
            task=row.task,
            version=row.version,
            learner=row.learner,
            role=row.role,
            score=row.score,
            finalized_by=row.finalized_by,
            finalized_by_login=row.finalized_by_login,
            contested=row.contested,
            adopted=True,
        )
    return rows


def newest_first(rows: list[Row]) -> list[Row]:
    return sorted(
        rows,
        key=lambda row: row.submission.submitted_at or row.submission.created_at,
        reverse=True,
    )


def summarize(rows: list[Row]) -> dict[str, object]:
    """コースのメニューに出す 1 行ぶんの概要。

    **試行は数えない**（#108）── ここに出るのは「何人が何件出したか」で、
    教員自身の動作確認はその問いの答えではない。
    """
    rows = [row for row in rows if not row.is_trial]
    latest: datetime | None = None
    for row in rows:
        at = row.submission.submitted_at or row.submission.created_at
        if latest is None or at > latest:
            latest = at
    return {
        "total": len(rows),
        "learners": len({str(row.submission.learner_id) for row in rows}),
        "latest": latest,
    }


__all__ = [
    "BUCKETS",
    "STATE_LABELS",
    "Distribution",
    "Filters",
    "Row",
    "distribution_of",
    "load_rows",
    "newest_first",
    "summarize",
]
