"""課題を別の問題セットへ移す・名前を変える・コピーする（2026-09-27）。

**課題キーは `<問題セット>/<名前>`。** 頭は問題セットを表すので、移したら
付け替える ── 付け替えないと、test5 にある課題のキーが `test4/…` のままになる
（実際に起きた。network の `test4/echoClient`）。

ところが課題 ID はキーから導かれる（`derived_id("tsk", コース, キー)`）ので、
**キーを変えるとは別の課題を作ること**である。そこで操作を 2 つに分ける:

    学生の提出が 0 件   移す（名前も変えられる）。キーを付け替え、元は消す
    学生の提出がある    移せない。名前を付けて**コピー**する（元は残す）

提出のある課題を移せないのは、採点結果が課題版を指しているから（P8）──
付け替えると、過去の成績が何の課題の点なのか辿れなくなる。**お試しの提出
（教員・TA の動作確認、`Submission.is_trial`）は妨げない。** 成績に数えない
もので、付け替えのときに消す（2026-09-27 の決定）。

写すのは**全版**。ID だけを付け替え、中身はそのまま運ぶ
（`aijudge_authoring.rekeyed_version`）── 学習者に出ている版・承認待ちの版の
区別も、出所も、そのまま残る。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aijudge_authoring import rekeyed_version
from aijudge_core import Task, TaskVersion
from aijudge_core.ids import CourseId, SubmissionId, TaskId, derived_id
from aijudge_course_admin.courses import _remove_artifacts
from aijudge_course_admin.errors import AdminError
from aijudge_unit_of_work import Store

__all__ = [
    "Relocated",
    "compose_key",
    "copy_task",
    "key_name",
    "learner_submission_count",
    "move_task",
]


def compose_key(unit: str, name: str) -> str:
    """問題セットの鍵と、その中での名前を繋ぐ。

    `ex02` + `p8` → `ex02/p8`。まとまりが無い課題（未分類）は後半だけを
    鍵にする。後半が既に `<問題セット>/` で始まっていれば重ねない ── 取り込み
    済みの課題と鍵を揃えたい場合があり、そこで縛ると直す手段が無くなる。
    """
    if not name:
        return ""
    if not unit or name.startswith(f"{unit}/"):
        return name
    return f"{unit}/{name}"


def key_name(key: str, unit: str | None) -> str:
    """キーから問題セットの頭を外した名前。

    頭がいまの問題セットと違う（移したのに付け替わっていない古い課題）
    場合も、最後の `/` の後ろを名前とみなす。
    """
    if unit and key.startswith(f"{unit}/"):
        return key[len(unit) + 1 :]
    return key.rsplit("/", 1)[-1]


def learner_submission_count(database: Store, task_id: TaskId) -> int:
    """学生の提出の数（お試しの提出は数えない）。**移せるかどうかはこれで決まる。**

    画面が移動とコピーのどちらを出すかを決めるのに使う。判定そのものは
    `move_task` がもう一度する ── 画面の表示だけを境界にしない（#146 と同じ）。
    """
    with database.unit_of_work() as uow:
        version_ids = [version.id for version in uow.tasks.list_versions(task_id)]
        submissions = uow.submissions.list_for_versions(version_ids) if version_ids else ()
    return sum(1 for submission in submissions if not submission.is_trial)


@dataclass(frozen=True)
class Relocated:
    """移した（コピーした）結果。**何が起きたかを数で返す。**"""

    task: Task
    previous_key: str
    key: str
    #: 付け替えのときに消したお試しの提出の数（コピーでは 0）。
    trial_submissions: int = 0


def move_task(
    database: Store,
    *,
    task_id: TaskId,
    unit: str,
    name: str = "",
    artifact_store: object | None = None,
) -> Relocated:
    """課題を `unit` へ移し、キーを `<unit>/<name>` に付け替える。

    `name` を省くといまの名前のまま。同じ問題セットのまま `name` だけ変えれば
    名前の変更になる。**学生の提出がある課題は断る**（コピーを案内する）。
    """
    unit = unit.strip()
    if not unit:
        raise AdminError("移動先の問題セットを選んでください")
    with database.unit_of_work() as uow:
        task, key, versions = _current(uow, task_id)
        new_key = compose_key(unit, (name.strip() or key_name(key, task.unit)))
        if new_key == key:
            raise AdminError("移動先と名前がいまと同じです")
        _refuse_taken(uow, task.course_id, new_key)

        submissions = uow.submissions.list_for_versions([version.id for version in versions])
        learner = [submission for submission in submissions if not submission.is_trial]
        if learner:
            raise AdminError(
                f"この課題には学生の提出が {len(learner)} 件あるので移せません。"
                "課題 ID はキーから導かれるので、移すと過去の成績が何の課題の点なのか"
                "辿れなくなります。別の問題セットで使うなら「コピー」を使ってください。"
            )
        trial_ids: list[SubmissionId] = [submission.id for submission in submissions]
        artifact_keys = [
            artifact.storage_key for submission in submissions for artifact in submission.artifacts
        ]

        moved = _rebuild(uow, task, versions, key=new_key, unit=unit)
        # 改訂の下書き（#306）は元の課題を指している。採用すると `spec.key` で
        # 保存されるので、キーも一緒に付け替えないと元のキーで課題が蘇る。
        for draft in uow.tasks.list_drafts(task.course_id):
            if draft.task_id == task.id:
                uow.tasks.save_draft(
                    draft.model_copy(
                        update={
                            "task_id": moved.id,
                            "spec": draft.spec.model_copy(update={"key": new_key}),
                        }
                    )
                )
        # 提出 → 元の課題の順に消す（`courses.delete_course` と同じ順）。
        uow.submissions.delete(trial_ids)
        uow.tasks.delete_task(task.id)
        uow.commit()

    _remove_artifacts(artifact_store, artifact_keys)
    return Relocated(task=moved, previous_key=key, key=new_key, trial_submissions=len(trial_ids))


def copy_task(database: Store, *, task_id: TaskId, unit: str, name: str) -> Relocated:
    """課題を `<unit>/<name>` の新しい課題として写す。**元は残す。**

    提出のある課題を別の問題セットでも出すための経路。写すのは中身（全版）と
    課題の設定で、提出・採点は写さない（それは元の課題のもの）。
    """
    unit = unit.strip()
    if not unit:
        raise AdminError("コピー先の問題セットを選んでください")
    if not name.strip():
        raise AdminError("コピーの名前を入力してください")
    with database.unit_of_work() as uow:
        task, key, versions = _current(uow, task_id)
        new_key = compose_key(unit, name.strip())
        _refuse_taken(uow, task.course_id, new_key)
        copied = _rebuild(uow, task, versions, key=new_key, unit=unit, withdrawn=False)
        uow.commit()
    return Relocated(task=copied, previous_key=key, key=new_key)


def _current(uow: Any, task_id: TaskId) -> tuple[Task, str, tuple[TaskVersion, ...]]:
    task = uow.tasks.get_task(task_id)
    if task is None:
        raise AdminError(f"課題 {task_id!r} がありません")
    versions = uow.tasks.list_versions(task_id)
    if not versions:
        raise AdminError("この課題には版がありません")
    # `list_versions` は新しい順。鍵は最新の版のものを使う（古い版は鍵を持たないことがある）。
    key = max(versions, key=lambda version: version.version).source_key
    if not key:
        # 鍵が無い版は ID を導き直せない（鍵は課題の同一性の素材）。
        raise AdminError("この課題にはキーが記録されていないので、移動もコピーもできません")
    return task, str(key), versions


def _refuse_taken(uow: Any, course_id: CourseId, key: str) -> None:
    if uow.tasks.get_task(TaskId(derived_id("tsk", str(course_id), key))) is not None:
        raise AdminError(f"キー {key} はこのコースの別の課題が使っています")


def _rebuild(
    uow: Any,
    task: Task,
    versions: tuple[TaskVersion, ...],
    *,
    key: str,
    unit: str,
    withdrawn: bool | None = None,
) -> Task:
    """新しいキーで課題と全版（と検査の記録）を保存し、新しい課題を返す。"""
    course_id = task.course_id
    rekeyed = {
        version.id: rekeyed_version(version, course_id=course_id, key=key) for version in versions
    }
    new_id = next(iter(rekeyed.values())).task_id

    siblings = [
        other
        for other in uow.tasks.list_for_course(course_id)
        if other.unit == unit and other.id != task.id
    ]
    update: dict[str, object] = {
        "id": new_id,
        "unit": unit,
        "current_version_id": (
            rekeyed[task.current_version_id].id
            if task.current_version_id in rekeyed
            else task.current_version_id
        ),
    }
    if withdrawn is not None:
        update["withdrawn"] = withdrawn
    if unit != task.unit:
        # **日程は移った先に揃える。** セットの中で締切がずれると「この回は
        # いつまでか」が言えなくなる（`set_unit_schedule` と同じ理由）。空の
        # セットへ移すなら、揃える相手が居ないのでいまの日程のまま入る。
        head = min(siblings, key=lambda item: item.sort_key) if siblings else None
        if head is not None:
            update |= {
                "session": head.session,
                "opens_at": head.opens_at,
                "submissions_open_at": head.submissions_open_at,
                "due_at": head.due_at,
                "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
            }
        # 並びは移動先の末尾。**番号を持たない課題も数に入れる**（#484 の守り）。
        positions = [other.position for other in siblings if other.position is not None]
        update["position"] = max(len(siblings), max(positions, default=0)) + 1

    rebuilt = task.model_copy(update=update)
    for old_id, version in rekeyed.items():
        uow.tasks.save_version(version)
        checks = uow.tasks.get_checks(old_id)
        if checks is not None:
            uow.tasks.save_checks(version.id, checks)
    uow.tasks.save_task(rebuilt)
    return rebuilt
