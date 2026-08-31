"""課題を消す・取り下げる。

**知識要素と同じ区別を課題にも当てる**（`aijudge_admin.kc`）。

    一度も使われていない  削除できる（打ち間違いの後始末）
    使われた              取り下げる（学習者に出さない。記録は残る）

採点結果は課題版を指している（P8）。提出のある課題を本当に消すと、過去の
成績が何の課題の点なのか辿れなくなり、その課題で積み上げた習熟度の出所も
失われる。**「使わなくする」と「無かったことにする」は別の操作である。**
"""

from __future__ import annotations

from aijudge_core import Task
from aijudge_core.ids import TaskId
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


__all__ = ["delete", "withdraw"]
