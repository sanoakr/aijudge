"""コースを「項目ごとの値」に開いて見せる（定義ファイルと DB の同期のため）。

`course diff` は課題ごとに「違う／同じ」しか言わず、課題の日程・問題セットの
値・コースの値は見ていない。定義ファイルを正本にしつつ、授業の運営では
コンソールから DB を直すこともある ── 両方が動く前提で揃えるには、
**どの課題のどの項目が、いつ変わったか**まで要る。

    aijudge-admin course snapshot --file course.yaml   # 定義ファイル側
    aijudge-admin course snapshot --course crs_...     # DB 側（更新時刻つき）

**両側を同じ規則で開く。** 版に入る項目は、どちらも `build_task_version` を
通した後の形（`content`）で比べる。定義ファイルの書き方（`problem_dir` か
`statement_file` か、日程を回に書くか課題に書くか）は、ここで消える。
`course export` の書き出しの形と `course.yaml` の手書きの形を文字どおり
比べると、同じ内容でも毎回「違う」になる（2026-09-24・2026-09-27 に実際に
取り違えた）。

**定義ファイルが書いていない値は出さない。** `course apply` は、書かれて
いない日程・問題セットの値を既存の値のまま残す（`save_task`・
`_apply_unit_settings`）。書いていない値は定義ファイルの管理外なので、
同期の対象にもしない。

DB 側の `changed_at` は、その項目がいまの値になった時刻である。

    版の項目      いまの値を持つ版のうち、いちばん古い版の `created_at`
    課題の項目    監査記録（`task.updated`）で、その項目を変えた最後の時刻

監査記録に無い変更（`course apply` による書き込みなど）は `null` になる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from aijudge_audit import AuditAction
from aijudge_authoring import TaskSpec, build_task_version, content
from aijudge_core import Course, Task, TaskVersion, normalize_suffixes
from aijudge_core.ids import CourseId, TenantId
from aijudge_course_admin.authoring import _title_of
from aijudge_course_admin.course_definition import UNIT_SCHEDULE_KEYS, load_course_definition
from aijudge_course_admin.course_export import _export_spec
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.operations import _IMPORTER, course_id_for
from aijudge_unit_of_work import Store

__all__ = ["SNAPSHOT_FORMAT", "snapshot_course", "snapshot_definition"]

# 出力の形の版。同期する側がこれを見て、読めない形なら止まる。
SNAPSHOT_FORMAT = 1

# 版の項目のうち、内容ではないもの。課題 ID・鍵は課題の同一性、出所は
# 誰が書いたかで、どちらも同期する値ではない。
_VERSION_EXCLUDED = frozenset({"task_id", "source_key", "provenance"})
# 課題の項目のうち、定義ファイルが常に書くもの（`save_task` が spec から取る）。
_TASK_ALWAYS = ("title", "unit", "session")
# 書かれたときだけ上書きする項目（`save_task`・#234・#491）。日程も同じ扱い。
# 位置も、書かなければいまの位置を残す（#484）── 位置を書かない定義（network の
# ように問題ディレクトリ名から位置が決まらないもの）を「位置が空」と読むと、
# 位置を埋めた DB と毎回食い違う（実際に同期の初回判定が止まった・2026-09-27）。
_TASK_IF_WRITTEN = ("position", "accepted_suffixes", "case_timeout_seconds", *UNIT_SCHEDULE_KEYS)
# 問題セットの値（`course_definition._UNIT_SETTING_KEYS` と同じ並び）。
_UNIT_SETTINGS = (
    "answer_mode",
    "editor_completion",
    "file_upload",
    "confidential_until_open",
    "campus_only",
    "clear_points",
    "late_penalty_steps",
)
_COURSE_FIELDS = ("description", "upload_suffixes", "knowledge_components")


def _plain(value: Any) -> Any:
    """JSON にそのまま載る形へ。**時刻は UTC に揃える** ── `+09:00` と `Z` の
    書き方の違いで同じ時刻が「違う」にならないように。"""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _version_values(version: TaskVersion) -> dict[str, Any]:
    return {
        name: _plain(value)
        for name, value in content(version).items()
        if name not in _VERSION_EXCLUDED
    }


def _file_task_values(
    spec: TaskSpec, settings: dict[str, Any], *, course_id: CourseId, profile: str
) -> dict[str, Any]:
    version = build_task_version(
        spec,
        course_id=course_id,
        subject_profile=spec.subject_profile or profile,
        authored_by=_IMPORTER,
    )
    values = _version_values(version)
    task = {
        "title": spec.title or _title_of(spec),
        "unit": spec.unit,
        "session": spec.session,
    }
    for name, value in task.items():
        values[f"task.{name}"] = _plain(value)
    # **書かれていれば**管理する（書かれていなければ `save_task` が既存の値を残す）。
    # 保存と同じ正規化を通す ── 通さないと `PNG` と `.png` が「違う」になる。
    written = {
        "position": spec.position,
        "accepted_suffixes": normalize_suffixes(spec.accepted_suffixes),
        "case_timeout_seconds": spec.case_timeout_seconds,
    } | {name: getattr(spec, name) for name in UNIT_SCHEDULE_KEYS}
    for name, value in written.items():
        if value:
            values[f"task.{name}"] = _plain(value)
    for name, value in settings.items():
        values[f"unit.{name}"] = _plain(value)
    return values


def snapshot_definition(path: Path, *, tenant_id: TenantId) -> dict[str, Any]:
    """定義ファイル側。**DB には触れない。**"""
    definition = load_course_definition(path)
    spec = definition.course
    course_id = course_id_for(tenant_id, spec["code"], spec["term"])
    tasks: dict[str, Any] = {}
    for task in definition.tasks:
        settings = definition.unit_settings.get(task.unit or "", {})
        tasks[task.key] = {
            "values": _file_task_values(
                task, settings, course_id=course_id, profile=spec["subject_profile"]
            ),
            "spec": task.model_dump(mode="json", exclude_none=True),
        }
    return {
        "format": SNAPSHOT_FORMAT,
        "source": "file",
        "course_id": str(course_id),
        "course": {name: _plain(spec[name]) for name in _COURSE_FIELDS if name in spec},
        "tasks": tasks,
    }


def _version_changed_at(versions: tuple[TaskVersion, ...]) -> dict[str, str | None]:
    """版の項目ごとに、いまの値になった時刻。`versions` は新しい順。"""
    latest = _version_values(versions[0])
    changed: dict[str, str | None] = {}
    for name, value in latest.items():
        since = versions[0].created_at
        for older in versions[1:]:
            if _version_values(older).get(name) != value:
                break
            since = older.created_at
        changed[name] = _plain(since)
    return changed


def _task_changed_at(uow: Any, task: Task, course_id: CourseId) -> dict[str, str]:
    """課題の項目ごとに、監査記録にある最後の変更時刻。

    課題 1 件の変更（`target_type="task"`）と、問題セット単位の変更
    （`target_type="unit"`・`<コース ID>/<回>`）の両方を見る。
    """
    events = list(uow.audit.list_for_target("task", str(task.id), limit=500))
    if task.unit:
        events += uow.audit.list_for_target("unit", f"{course_id}/{task.unit}", limit=500)
    changed: dict[str, datetime] = {}
    for event in events:
        if event.action is not AuditAction.TASK_UPDATED:
            continue
        for name in (event.detail or {}).get("changed", {}) or {}:
            if name not in changed or event.at > changed[name]:
                changed[name] = event.at
    result: dict[str, str] = {}
    for name, at in changed.items():
        prefix = "unit" if name in _UNIT_SETTINGS else "task"
        result[f"{prefix}.{name}"] = _plain(at)
    return result


def _db_task_values(task: Task) -> dict[str, Any]:
    values = {
        f"task.{name}": _plain(getattr(task, name)) for name in (*_TASK_ALWAYS, *_TASK_IF_WRITTEN)
    }
    for name in _UNIT_SETTINGS:
        values[f"unit.{name}"] = _plain(getattr(task, name))
    return values


def snapshot_course(database: Store, course_id: CourseId) -> dict[str, Any]:
    """DB 側。**何も書かない。** 項目ごとの値と、いまの値になった時刻。"""
    with database.unit_of_work() as uow:
        course: Course | None = uow.identity.get_course(course_id)
        if course is None:
            raise AdminError(f"コースがありません: {course_id}")
        tasks: dict[str, Any] = {}
        for task in uow.tasks.list_for_course(course_id):
            versions = uow.tasks.list_versions(task.id)
            if not versions:
                continue
            latest = versions[0]
            if latest.source_key is None:
                # 鍵の無い版（古い取り込み）は定義ファイルと突き合わせられない。
                continue
            values = _version_values(latest) | _db_task_values(task)
            changed_at: dict[str, str | None] = dict.fromkeys(values)
            changed_at |= _version_changed_at(versions)
            changed_at |= _task_changed_at(uow, task, course_id)
            try:
                spec = _export_spec(database, task, latest, course=course).model_dump(
                    mode="json", exclude_none=True
                )
            except AdminError:
                # 宣言に戻せない版（`_export_spec`）。値の比較はできるので落とさない。
                spec = None
            tasks[latest.source_key] = {
                "values": values,
                "changed_at": changed_at,
                "spec": spec,
                "withdrawn": task.withdrawn,
            }
        course_values = {name: _plain(getattr(course, name)) for name in _COURSE_FIELDS}
    return {
        "format": SNAPSHOT_FORMAT,
        "source": "db",
        "course_id": str(course_id),
        "taken_at": _plain(datetime.now(UTC)),
        "course": course_values,
        "tasks": tasks,
    }
