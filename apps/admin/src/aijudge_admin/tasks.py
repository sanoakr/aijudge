"""課題を消す・取り下げる。

**知識要素と同じ区別を課題にも当てる**（`aijudge_admin.kc`）。

    一度も使われていない  削除できる（打ち間違いの後始末）
    使われた              取り下げる（学習者に出さない。記録は残る）

採点結果は課題版を指している（P8）。提出のある課題を本当に消すと、過去の
成績が何の課題の点なのか辿れなくなり、その課題で積み上げた習熟度の出所も
失われる。**「使わなくする」と「無かったことにする」は別の操作である。**
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aijudge_core import Task
from aijudge_core.ids import CourseId, TaskId
from aijudge_persistence import Database

from .operations import AdminError


def withdraw(database: Database, *, task_id: TaskId, restore: bool = False) -> Task:
    """出題を取り下げる（`restore=True` で取り消す）。

    **消えない。** 学習者には出なくなるが、提出・採点・Q-matrix はそのまま
    残り、教員の一覧には印付きで並ぶ。押し間違いを直せるように、取り消しも
    同じ経路にしてある。
    """
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        if task is None:
            raise AdminError(f"課題 {task_id!r} がありません")
        updated = task.model_copy(update={"withdrawn": not restore})
        uow.tasks.save_task(updated)
        uow.commit()
    return updated


def delete(database: Database, *, task_id: TaskId) -> Task:
    """**提出が 1 件も無い課題だけを消す。**

    1 件でもあれば消さない ── 採点結果は課題版を指しており（P8）、消すと
    過去の成績が何の課題の点なのか辿れなくなる。使わなくするだけなら
    取り下げる。
    """
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        if task is None:
            raise AdminError(f"課題 {task_id!r} がありません")
        submissions = uow.tasks.submission_count(task_id)

    if submissions:
        raise AdminError(
            f"この課題には提出が {submissions} 件あります。"
            "消すと、その提出に付いた成績が何の課題の点なのか辿れなくなります。"
            "使わなくするだけなら「出題を取り下げる」を使ってください。"
        )

    with database.unit_of_work() as uow:
        uow.tasks.delete_task(task_id)
        uow.commit()
    return task


@dataclass
class UnitReport:
    """問題セット 1 つを片付けた（あるいは片付けるとどうなるかを見た）結果。

    **削除と取り下げを混ぜて数えない。** 1 回の操作で課題ごとに結果が違うので、
    件数だけを返すと「何がどうなったのか」が言えなくなる（#59）。
    """

    deleted: list[Task] = field(default_factory=list)
    withdrawn: list[Task] = field(default_factory=list)
    # 既に取り下げてあったもの。**取り下げ済みを「取り下げた」と数えない。**
    untouched: list[Task] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.deleted) + len(self.withdrawn) + len(self.untouched)


def clear_unit(
    database: Database, *, course_id: CourseId, unit: str | None, dry_run: bool = False
) -> UnitReport:
    """問題セットを丸ごと片付ける。**課題ごとに削除か取り下げかを振り分ける。**

    セットの中に提出のある課題と無い課題が混ざるのが普通なので、`delete` と
    `withdraw` の区別（一度も使われていないものは消す／使われたものは残して
    出さない）をそのままセット単位に持ち上げる。片方に揃えると、消せるはずの
    打ち間違いが残るか、消してはいけない成績の出所が消えるかのどちらかになる。

    `dry_run=True` は**何もせずに内訳だけ返す。** 押す前に「削除 N 件・
    取り下げ M 件」を出すためにある ── 結果が課題ごとに違う操作で、
    実行してからでないと分からないのでは確認にならない。
    """
    with database.unit_of_work() as uow:
        tasks = [
            task
            for task in uow.tasks.list_for_course(course_id)
            # `None` は「まとまりの名前が無い課題」。**空文字と同じにしない**
            # ── 画面はそれを回番号でまとめて 1 つのセットとして出しており、
            # そこからも片付けられる必要がある。
            if task.unit == unit
        ]
        if not tasks:
            raise AdminError(f"問題セット {unit!r} に課題がありません")
        counts = {task.id: uow.tasks.submission_count(task.id) for task in tasks}

    report = UnitReport()
    for task in sorted(tasks, key=lambda t: t.sort_key):
        if counts[task.id]:
            (report.untouched if task.withdrawn else report.withdrawn).append(task)
        else:
            report.deleted.append(task)
    if dry_run:
        return report

    for task in report.withdrawn:
        withdraw(database, task_id=task.id)
    for task in report.deleted:
        delete(database, task_id=task.id)
    return report


__all__ = ["UnitReport", "clear_unit", "delete", "withdraw"]
