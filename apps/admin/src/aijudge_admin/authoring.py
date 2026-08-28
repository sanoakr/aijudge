"""課題を足す操作。**画面・API・CLI がここを共有する。**

経路ごとに組み立て方が分かれると、「画面から作った課題だけ観点が 1 つ
足りない」が起きる。実際に起きた ── zip 取り込みだけ `readability_weight` が
0.0 固定で、画面から入れた課題には AI 観点が付かなかった（`TaskSpec` の
docstring 参照）。

**冪等。** 同じ `key` に同じ内容を入れ直しても課題は増えず、締切も消えない。
移行では同じディレクトリを何度も流すことになるので、これは必須の性質である。
内容が違う場合は拒否する ── 過去の採点基準を書き換えないため（P8）。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_authoring import TaskSpec, build_task_version
from aijudge_authoring.repository import TaskStoreError
from aijudge_core import Task, TaskVersion
from aijudge_core.ids import CourseId, UserId
from aijudge_persistence import Database

from .operations import AdminError


@dataclass(frozen=True)
class SavedTask:
    """保存の結果。**何が起きたかを呼び出し元に返す。**

    「保存しました」だけだと、流し込みが 100 件のうち何件を新しく作ったのか
    運用者に分からない。移行を二度流したときに気づけるようにする。
    """

    task: Task
    version: TaskVersion
    created: bool
    """この課題がこの呼び出しで初めて作られたか。"""

    @property
    def test_cases(self) -> int:
        return len(self.version.test_cases)

    @property
    def auto_graded(self) -> bool:
        return bool(self.version.test_cases)


def save_task(
    database: Database,
    *,
    course_id: CourseId,
    spec: TaskSpec,
    subject_profile: str,
    authored_by: UserId,
) -> SavedTask:
    """課題を保存する。既にあれば内容の同一性を確かめ、無ければ作る。"""
    version = build_task_version(
        spec, subject_profile=subject_profile, authored_by=authored_by
    )
    with database.unit_of_work() as uow:
        existing = uow.tasks.get_task(version.task_id)
        task = Task(
            id=version.task_id,
            course_id=course_id,
            title=spec.title or _title_of(spec),
            unit=spec.unit,
            session=spec.session,
            position=spec.position,
            # **入れ直しで締切を消さない。** 教員が画面で入れた値を、流し込みの
            # 再実行が黙って消すと成績の期限が飛ぶ。明示された場合だけ上書きする。
            opens_at=spec.opens_at or (existing.opens_at if existing else None),
            due_at=spec.due_at or (existing.due_at if existing else None),
        )
        uow.tasks.save_task(task)
        try:
            uow.tasks.save_version(version)
        except TaskStoreError as exc:
            raise AdminError(
                f"{spec.key}: 保存済みの課題と内容が違います。問題文を直したなら"
                f"版を上げる必要があります（過去の採点基準は書き換えない、P8）: {exc}"
            ) from exc
        uow.commit()

    return SavedTask(task=task, version=version, created=existing is None)


def _title_of(spec: TaskSpec) -> str:
    """問題文の見出しから題名を取る。取れなければキーで代用する。

    題名が無いと一覧が課題 ID の羅列になり、教員がどれを触っているのか
    分からなくなる。
    """
    from aijudge_authoring.importers.sharif_judge import ImportError_, parse_title

    try:
        title, _tag = parse_title(spec.statement)
    except ImportError_:
        return spec.key
    return title


__all__ = ["SavedTask", "save_task"]
