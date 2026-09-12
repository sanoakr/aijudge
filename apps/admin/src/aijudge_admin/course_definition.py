"""コースの定義（YAML）を読んで投入する。

`demo seed` が `subjects/demo/course.yaml` でやっていることの一般化で、
どのコースも 1 つのファイルから作れるようにする（`course apply`）。

学期初めの手順は「コースを作り、課題を取り込み、日程を入れる」だが、
`course create` ＋ `task import` では画像で出す課題（認定証のスクリーン
ショット・環境構築の画面）が作れず、日程はブラウザで回ごとに入れ直す
ことになる。定義がファイルにあれば、次の年度はそのファイルを写して日付を
直すだけで済み、何を出したかの記録にもなる。

    course:
      code: network
      title: ネットワーク及び演習
      term: 2026-後期
      subject_profile: cs_network_python
      description: ...                      # 任意
    units:                                  # 任意。回ごとの日程の既定
      ex1: {opens_at: 2026-09-18T13:00:00+09:00, due_at: 2026-09-25T23:59:00+09:00}
    tasks:
      - key: ex1/cert                       # TaskSpec のフィールドをそのまま書く
        unit: ex1
        position: 1
        statement: |
          ## [必須] 認定証を提出する ##
          ...
        accepted_suffixes: [.png, .jpg, .jpeg, .pdf]
        criteria: [...]
      - problem_dir: ex1/p1                 # Sharif Judge 形式の問題ディレクトリ
        readability_weight: 0.3             # （YAML からの相対パス）

`problem_dir` を書いた課題は、`desc.md` を問題文、`in/` `out/` をテスト
ケース、`<name>.c` / `.py` を参照解答として読む（`importers/sharif_judge`
と同じ規則）。`key` `unit` `position` `session` はディレクトリ名から決まる。
YAML に同じフィールドがあればそちらが勝つ。**既存の課題ディレクトリを
そのまま使えること**が、この定義形式を移行元と同じ木に置ける条件である。

**何度流しても増えない。** `ensure_course` と `save_task` が冪等なので、
定義を直して流し直せば差分だけが入る。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aijudge_authoring import TaskSpec
from aijudge_authoring.importers import sharif_judge
from aijudge_core import Course
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

from .authoring import save_task
from .operations import AdminError, ensure_course

__all__ = ["AppliedCourse", "CourseDefinition", "apply_course_definition", "load_course_definition"]

# `course:` に書けるもののうち、`ensure_course` が受け取らない運用値。
_COURSE_REQUIRED = ("code", "title", "term", "subject_profile")
_COURSE_OPTIONAL = ("description", "upload_suffixes")
# 回ごとの既定として書ける日程。課題側に無ければここから埋める。
_UNIT_SCHEDULE_KEYS = ("opens_at", "due_at")
# 定義側だけの語彙。`TaskSpec` に渡す前に解決して消す。
_PROBLEM_DIR = "problem_dir"
_STATEMENT_FILE = "statement_file"


@dataclass(frozen=True)
class CourseDefinition:
    course: dict[str, Any]
    tasks: tuple[TaskSpec, ...]


@dataclass(frozen=True)
class AppliedCourse:
    course: Course
    tasks: int
    created: bool


def load_course_definition(path: Path) -> CourseDefinition:
    """YAML を読み、課題を `TaskSpec` の並びにする。**まだ何も保存しない。**"""
    if not path.is_file():
        raise AdminError(f"コースの定義がありません: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AdminError(f"コースの定義が対応表ではありません: {path}")

    spec = data.get("course") or {}
    for key in _COURSE_REQUIRED:
        if not spec.get(key):
            raise AdminError(f"コースの定義に {key} がありません: {path}")
    unknown = set(spec) - set(_COURSE_REQUIRED) - set(_COURSE_OPTIONAL)
    if unknown:
        raise AdminError(f"course に書けないフィールドです: {sorted(unknown)}（{path}）")

    units = data.get("units") or {}
    if not isinstance(units, dict):
        raise AdminError(f"units は回の名前を鍵にした対応表です: {path}")
    for unit, schedule in units.items():
        bad = set(schedule or {}) - set(_UNIT_SCHEDULE_KEYS)
        if bad:
            raise AdminError(f"units.{unit} に書けないフィールドです: {sorted(bad)}（{path}）")

    tasks = []
    for raw in data.get("tasks") or []:
        if not isinstance(raw, dict):
            raise AdminError(f"tasks の要素が対応表ではありません: {raw!r}（{path}）")
        tasks.append(_task_spec(dict(raw), base=path.parent, units=units))
    return CourseDefinition(course=dict(spec), tasks=tuple(tasks))


def _task_spec(raw: dict[str, Any], *, base: Path, units: dict[str, Any]) -> TaskSpec:
    problem_dir = raw.pop(_PROBLEM_DIR, None)
    statement_file = raw.pop(_STATEMENT_FILE, None)
    if problem_dir is not None:
        raw = _from_problem_dir(raw, _under(base, problem_dir))
    if statement_file is not None:
        raw.setdefault("statement", _under(base, statement_file).read_text(encoding="utf-8"))

    # 回ごとの日程の既定。**課題側の指定が勝つ。**
    schedule = units.get(raw.get("unit")) or {}
    for key in _UNIT_SCHEDULE_KEYS:
        if raw.get(key) is None and schedule.get(key) is not None:
            raw[key] = schedule[key]

    try:
        return TaskSpec.model_validate(raw)
    except ValueError as exc:
        raise AdminError(f"課題 {raw.get('key', '?')} の定義が不正です: {exc}") from exc


def _under(base: Path, relative: str) -> Path:
    """定義ファイルの下だけを指せる。上に抜ける相対パスは受け付けない。"""
    target = (base / relative).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        raise AdminError(f"{relative!r} は定義ファイルの外を指しています")
    return target


def _from_problem_dir(raw: dict[str, Any], problem_dir: Path) -> dict[str, Any]:
    """Sharif Judge 形式の問題ディレクトリから、YAML に無いものを埋める。"""
    if not problem_dir.is_dir():
        raise AdminError(f"問題ディレクトリがありません: {problem_dir}")
    desc = problem_dir / "desc.md"
    if "statement" not in raw:
        if not desc.is_file():
            raise AdminError(f"{problem_dir} に desc.md がありません")
        raw["statement"] = desc.read_text(encoding="utf-8")

    unit, session, position = sharif_judge.parse_unit(problem_dir)
    raw.setdefault("key", f"{unit}/{problem_dir.name}")
    raw.setdefault("unit", unit)
    if session is not None:
        raw.setdefault("session", session)
    if position is not None:
        raw.setdefault("position", position)

    if "reference_solution" not in raw:
        reference = sharif_judge.find_reference_solution(problem_dir)
        if reference is not None:
            raw["reference_solution"] = reference

    if "test_cases" not in raw:
        try:
            cases = sharif_judge.collect_test_cases(problem_dir)
        except sharif_judge.ImportError_ as exc:
            raise AdminError(f"{problem_dir} のテストケースが壊れています: {exc}") from exc
        raw["test_cases"] = [
            {
                "name": case.name,
                "input": case.payload["input"],
                "expected": case.payload["expected"],
                "hidden": case.hidden,
                "weight": case.weight,
            }
            for case in cases
        ]
    return raw


def apply_course_definition(
    database: Database,
    path: Path,
    *,
    tenant_id: TenantId,
    profiles_dir: Path,
    authored_by: UserId,
) -> AppliedCourse:
    """定義を読んでコースと課題を作る。**何度走らせても増えない。**"""
    definition = load_course_definition(path)
    spec = definition.course

    course, created = ensure_course(
        database,
        tenant_id=tenant_id,
        code=spec["code"],
        title=spec["title"],
        term=spec["term"],
        subject_profile=spec["subject_profile"],
        profiles_dir=profiles_dir,
    )

    # 運用値（概要・提出できる形式）は `ensure_course` の外で入れる。
    # あちらはコースの同一性と採点の器を用意する関数で、ブラウザからも
    # 編集される値は、定義が書いたときだけ上書きする。
    updates: dict[str, Any] = {}
    if spec.get("description") is not None:
        updates["description"] = spec["description"]
    if spec.get("upload_suffixes"):
        updates["upload_suffixes"] = tuple(spec["upload_suffixes"])
    if updates:
        with database.unit_of_work() as uow:
            stored = uow.identity.get_course(course.id)
            course = (stored or course).model_copy(update=updates)
            uow.identity.save_course(course)
            uow.commit()

    for task in definition.tasks:
        save_task(
            database,
            course_id=course.id,
            spec=task,
            # 課題が自分のプロファイルを持つ（#195）。ここで渡すのは既定で、
            # 定義が `subject_profile` を書いていればそちらが勝つ。
            subject_profile=course.subject_profile,
            authored_by=authored_by,
        )
    return AppliedCourse(course=course, tasks=len(definition.tasks), created=created)
