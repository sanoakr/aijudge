"""束を入れると何が起きるかを、**入れる前に**言う（#161）。

まとまった投入で怖いのは「押したら何件変わったか分からない」ことである。
だから読み終えた `TaskSpec` を保存する前に、1 件ずつ

    新規      この鍵の課題はまだ無い
    変化なし  保存済みと中身が同じ（入れ直しても何も起きない）
    版が上がる 保存済みと中身が違う（過去の採点基準は書き換えない、P8）

のどれになるかを数える。**ここでも保存はしない。**
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aijudge_authoring import TaskSpec, build_task_version, substantive
from aijudge_core.ids import CourseId, UserId
from aijudge_persistence import Database

from .authoring import _title_of


class PlannedChange(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    REVISED = "revised"


@dataclass(frozen=True)
class PlannedTask:
    """1 件ぶんの見込み。**鍵をそのまま出す** ── 取り違えはここで気づける。"""

    spec: TaskSpec
    change: PlannedChange
    title: str
    existing_version: int | None = None


def plan_bundle(
    database: Database,
    *,
    course_id: CourseId,
    subject_profile: str,
    authored_by: UserId,
    specs: tuple[TaskSpec, ...],
) -> tuple[PlannedTask, ...]:
    """保存したらどうなるかを数える。**何も書かない。**

    判定は `save_task` と同じ規則（`substantive` の比較）に揃える ──
    別の規則で数えると、「変化なし」と出したものが保存で版を上げる。

    **`authored_by` は保存で使う値をそのまま渡すこと。** `substantive` は
    `provenance.authored_by` を含むので、別の値で数えると、同じ束なのに
    全件が「版が上がる」に見える（実際にそうなり、テストが捕まえた）。
    """
    planned: list[PlannedTask] = []
    with database.unit_of_work() as uow:
        for spec in specs:
            candidate = build_task_version(
                spec,
                course_id=course_id,
                subject_profile=subject_profile,
                authored_by=authored_by,
            )
            existing = uow.tasks.latest_version(candidate.task_id)
            if existing is None:
                change = PlannedChange.NEW
            elif substantive(existing) == substantive(candidate):
                change = PlannedChange.UNCHANGED
            else:
                change = PlannedChange.REVISED
            planned.append(
                PlannedTask(
                    spec=spec,
                    change=change,
                    # 題名の取り方も `save_task` と同じにする（画面に出る名前が
                    # 保存前と保存後で変わらないように）。
                    title=spec.title or _title_of(spec),
                    existing_version=None if existing is None else existing.version,
                )
            )
    return tuple(planned)


__all__ = ["PlannedChange", "PlannedTask", "plan_bundle"]
