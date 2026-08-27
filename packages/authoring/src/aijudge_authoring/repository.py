"""課題の保存先（S2）。

課題は S2 の持ち物なので、保存先の契約もここに置く。採点側（S5）と
提出側（S3）は課題を作らず読むだけで、両者が別々に取り込むと
「表示している観点」と「採点した観点」が食い違いうる。

`TaskVersion` は公開後不変（P8）。保存済みの版を書き換えようとしたら
拒否する。問題文の訂正は新しい版を作る。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aijudge_core import Task, TaskVersion
from aijudge_core.ids import CourseId, TaskId, TaskVersionId


class TaskStoreError(Exception):
    """課題の保存先の不整合。"""


class TaskImmutabilityViolation(TaskStoreError):
    """公開済みの TaskVersion を書き換えようとした（P8）。"""


@runtime_checkable
class TaskRepository(Protocol):
    """課題と課題版の保存先。"""

    def save_task(self, task: Task) -> None: ...

    def get_task(self, task_id: TaskId) -> Task | None: ...

    def save_version(self, version: TaskVersion) -> None:
        """課題版を保存する。同じ ID の版が既にあれば拒否する（P8）。"""
        ...

    def get_version(self, version_id: TaskVersionId) -> TaskVersion | None: ...

    def latest_version(self, task_id: TaskId) -> TaskVersion | None: ...

    def list_for_course(self, course_id: CourseId) -> tuple[Task, ...]:
        """コースの課題一覧。学生 UI と教員 UI が使う。"""
        ...


class InMemoryTaskRepository:
    """テストと開発用。規則は本番と同じにしてある。"""

    def __init__(self) -> None:
        self._tasks: dict[TaskId, Task] = {}
        self._versions: dict[TaskVersionId, TaskVersion] = {}
        self._order: list[TaskVersionId] = []

    def save_task(self, task: Task) -> None:
        self._tasks[task.id] = task

    def get_task(self, task_id: TaskId) -> Task | None:
        return self._tasks.get(task_id)

    def save_version(self, version: TaskVersion) -> None:
        existing = self._versions.get(version.id)
        if existing is not None:
            if existing == version:
                # 同じ内容の取り込みを繰り返すのは冪等な操作。
                # 決定的 ID（derived_id）で取り込む経路では普通に起きる。
                return
            raise TaskImmutabilityViolation(
                f"TaskVersion {version.id} already exists with different content; "
                "corrections create a new version (P8)"
            )
        self._versions[version.id] = version
        self._order.append(version.id)

    def get_version(self, version_id: TaskVersionId) -> TaskVersion | None:
        return self._versions.get(version_id)

    def latest_version(self, task_id: TaskId) -> TaskVersion | None:
        versions = [
            self._versions[vid] for vid in self._order if self._versions[vid].task_id == task_id
        ]
        if not versions:
            return None
        return max(versions, key=lambda v: v.version)

    def list_for_course(self, course_id: CourseId) -> tuple[Task, ...]:
        return tuple(
            sorted(
                (task for task in self._tasks.values() if task.course_id == course_id),
                key=lambda task: task.id,
            )
        )
