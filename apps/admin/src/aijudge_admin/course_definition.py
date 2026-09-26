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
      knowledge_components: [cs.loops.control.basic, ...]   # 任意。課題が問うものは自動で入る
    units:                                  # 任意。回ごとの日程の既定
      ex1: {opens_at: 2026-09-18T13:00:00+09:00, due_at: 2026-09-25T23:59:00+09:00}
      ex2: {opens_at: 2026-09-25T16:00:00+09:00,           # 課題文は先に配り、
            submissions_open_at: 2026-09-25T16:30:00+09:00,  # 提出は演習時間から
            due_at: 2026-10-02T12:00:00+09:00}
      test2: {grading_starts_at: 2026-09-25T16:45:00+09:00,    # 試験の採点開始と
              accepts_until: 2026-09-25T16:45:00+09:00}        # 受付終了
      ex5: {answer_mode: editor, editor_completion: true}   # 答え方（ADR 0026）
      ex3: {answer_mode: editor, file_upload: false}        # エディタだけ（試験）
      test3: {confidential_until_open: true}                # 公開まで教員だけ（TA にも見せない）
      ex4: {clear_points: 60}                               # 合計 60 点でクリア
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

`answer_mode`・`editor_completion`・`file_upload`・`confidential_until_open`・`clear_points` は
**問題セットの値**で、`/manage` の切り替えと同じく回の全課題に入れる（課題ごとには
書けない）。書いた回だけを変え、書かない回は画面で切り替えた値を残す。`editor` にできない課題
（提出形式に `.c`・`.py`・`.md` が無い）があれば投入を止める。

`problem_dir` を書いた課題は、`desc.md` を問題文、`in/` `out/` をテスト
ケース、`<name>.c` / `.py` を参照解答として読む（`importers/sharif_judge`
と同じ規則）。`key` `unit` `position` `session` はディレクトリ名から決まる。
YAML に同じフィールドがあればそちらが勝つ。**既存の課題ディレクトリを
そのまま使えること**が、この定義形式を移行元と同じ木に置ける条件である。

**何度流しても増えない。** `ensure_course` と `save_task` が冪等なので、
定義を直して流し直せば差分だけが入る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aijudge_authoring import TaskSpec
from aijudge_authoring.importers import sharif_judge
from aijudge_core import AnswerMode, Course, Task
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

from .answer_mode import editor_blockers, file_upload_required
from .authoring import save_task
from .operations import AdminError, ensure_course

__all__ = [
    "AppliedCourse",
    "CourseDefinition",
    "apply_course_definition",
    "course_template",
    "load_course_definition",
]

# 教員に渡すひな形。**説明のコメントごと配る** ── 形式の説明はこのファイル
# 冒頭にあるが、教員はリポジトリを開かない。画面からダウンロードして埋め、
# 管理者に渡す（`course apply` を流すのは管理者）。
TEMPLATE_PATH = Path(__file__).with_name("course_template.yaml")


def course_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


# `course:` に書けるもののうち、`ensure_course` が受け取らない運用値。
_COURSE_REQUIRED = ("code", "title", "term", "subject_profile")
_COURSE_OPTIONAL = ("description", "upload_suffixes", "knowledge_components")
# 回ごとの既定として書ける日程。課題側に無ければここから埋める。
# 提出開始・採点開始・受付終了も同じ扱い（`TaskSpec` の同名の欄）。
UNIT_SCHEDULE_KEYS = (
    "opens_at",
    "submissions_open_at",
    "due_at",
    "grading_starts_at",
    "accepts_until",
)
# 回（問題セット）の値として書けるもの。**課題ごとには書けない** ──
# `/manage` と同じく回の全課題に入れる。同じ回で答え方が混ざると、学習者は
# 課題ごとに画面を行き来することになる（ADR 0026）。
#
# `confidential_until_open`（公開まで教員だけに見せる・試験）もここに置く。画面でしか
# 入れられないと、`course apply` で課題を作ってからコンソールで切り替えるまでの間、
# **公開前の課題が TA に見えている**（`docs/design/task-visibility.md` の B が塞ぎたい
# 漏洩そのもの）。定義から入れれば、課題の保存と同じ実行の中で入る。
#
# `clear_points`（問題セットのクリア点・2026-09-25）も同じ。合計の点数で書く。
_UNIT_SETTING_KEYS = (
    "answer_mode",
    "editor_completion",
    "file_upload",
    "confidential_until_open",
    "clear_points",
)
# 定義側だけの語彙。`TaskSpec` に渡す前に解決して消す。
_PROBLEM_DIR = "problem_dir"
_STATEMENT_FILE = "statement_file"


@dataclass(frozen=True)
class CourseDefinition:
    course: dict[str, Any]
    tasks: tuple[TaskSpec, ...]
    # 回 → 問題セットの値（`_UNIT_SETTING_KEYS`）。書かれた回だけが入る。
    unit_settings: dict[str, dict[str, Any]] = field(default_factory=dict)


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
    unit_settings: dict[str, dict[str, Any]] = {}
    for unit, schedule in units.items():
        bad = set(schedule or {}) - set(UNIT_SCHEDULE_KEYS) - set(_UNIT_SETTING_KEYS)
        if bad:
            raise AdminError(f"units.{unit} に書けないフィールドです: {sorted(bad)}（{path}）")
        settings = _unit_settings(str(unit), schedule or {}, path)
        if settings:
            unit_settings[str(unit)] = settings

    tasks = []
    for raw in data.get("tasks") or []:
        if not isinstance(raw, dict):
            raise AdminError(f"tasks の要素が対応表ではありません: {raw!r}（{path}）")
        tasks.append(_task_spec(dict(raw), base=path.parent, units=units))
    return CourseDefinition(course=dict(spec), tasks=tuple(tasks), unit_settings=unit_settings)


def _unit_settings(unit: str, raw: dict[str, Any], path: Path) -> dict[str, Any]:
    """回の値を型に直す。**読む段で落とす** ── 課題を入れてから不正と分かると、
    半分だけ入った定義が残る。"""
    settings: dict[str, Any] = {}
    if "answer_mode" in raw:
        try:
            settings["answer_mode"] = AnswerMode(str(raw["answer_mode"]))
        except ValueError:
            wanted = "・".join(mode.value for mode in AnswerMode)
            raise AdminError(
                f"units.{unit}.answer_mode は {wanted} のどれかです"
                f": {raw['answer_mode']!r}（{path}）"
            ) from None
    if "clear_points" in raw:
        value = raw["clear_points"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float) or value <= 0
        ):
            raise AdminError(f"units.{unit}.clear_points は正の数か null です: {value!r}（{path}）")
        settings["clear_points"] = None if value is None else float(value)
    for flag in ("editor_completion", "file_upload", "confidential_until_open"):
        if flag not in raw:
            continue
        if not isinstance(raw[flag], bool):
            raise AdminError(f"units.{unit}.{flag} は true か false です: {raw[flag]!r}（{path}）")
        settings[flag] = raw[flag]
    if (
        settings.get("file_upload") is False
        and settings.get("answer_mode") is not AnswerMode.EDITOR
    ):
        # エディタも無くファイルも断ると誰も提出できない（`Task._check_answer_paths`）。
        raise AdminError(
            f"units.{unit}.file_upload: false はエディタの回（answer_mode: editor）にだけ"
            f"書けます（{path}）"
        )
    return settings


def _task_spec(raw: dict[str, Any], *, base: Path, units: dict[str, Any]) -> TaskSpec:
    problem_dir = raw.pop(_PROBLEM_DIR, None)
    statement_file = raw.pop(_STATEMENT_FILE, None)
    if problem_dir is not None:
        raw = _from_problem_dir(raw, _under(base, problem_dir))
    if statement_file is not None:
        raw.setdefault("statement", _under(base, statement_file).read_text(encoding="utf-8"))

    # 回ごとの日程の既定。**課題側の指定が勝つ。**
    schedule = units.get(raw.get("unit")) or {}
    for key in UNIT_SCHEDULE_KEYS:
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
    revise: bool = False,
) -> AppliedCourse:
    """定義を読んでコースと課題を作る。**何度走らせても増えない。**

    `revise` は**訂正**。出題済みの課題の内容を変えるときに要る ── 既定では
    保存済みと内容が違えば拒む（過去の採点がどの基準で付いたか辿れなくなる
    ため・P8）。訂正では版を 1 つ上げた `TaskVersion` を作る。

    **既存の提出と採点は動かない。** 提出は自分が出された版を指しており、
    新しい版はそれ以降の提出にだけ効く。内容が変わっていない課題は版を
    上げないので、`--revise` を付けても増えるのは実際に直した課題だけである。
    """
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
    # **コースが使う知識要素は、定義が書いたものと課題が問うものの和**（#289）。
    # 課題は範囲にある知識要素しか名指しできない（`kc.assert_registered`）ので、
    # 定義に課題側の KC だけを書いても通るように、先に範囲へ入れる。既存の
    # 範囲は残す ── 流し直しで教員が画面から足したものを消さない。
    declared = {str(key) for key in spec.get("knowledge_components") or ()}
    declared |= {key for task in definition.tasks for key in task.knowledge_components}
    if declared - set(course.knowledge_components):
        updates["knowledge_components"] = tuple(sorted(set(course.knowledge_components) | declared))
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
            revise=revise,
        )
    _apply_unit_settings(database, course, definition.unit_settings)
    return AppliedCourse(course=course, tasks=len(definition.tasks), created=created)


def _apply_unit_settings(
    database: Database, course: Course, unit_settings: dict[str, dict[str, Any]]
) -> None:
    """回の値を、その回の全課題に入れる（`/manage` の切り替えと同じ）。

    **`editor` の検査は画面と同じ関数**（`editor_blockers`）で行う。定義から
    入れる経路だけ検査を抜けると、学習者が何も提出できない課題ができる。

    検査は課題を保存した後に走る（提出形式は保存済みの値で決まる）。断った
    ときは課題だけが入り、答え方は変わらない ── 定義を直して流し直せば
    冪等に揃う。
    """
    if not unit_settings:
        return
    # **秘匿は先に、単独で入れる。** 下の `editor` の検査で断ると同じ作業単位が
    # 丸ごと巻き戻る ── 課題は保存済みなので、秘匿だけが入らないまま TA に
    # 見える課題が残る。秘匿は検査を要しない値なので、断られうる値と束ねない。
    confidential = {
        unit: {"confidential_until_open": settings["confidential_until_open"]}
        for unit, settings in unit_settings.items()
        if "confidential_until_open" in settings
    }
    if confidential:
        with database.unit_of_work() as uow:
            tasks = uow.tasks.list_for_course(course.id)
            for unit, settings in confidential.items():
                for task in tasks:
                    if task.unit == unit:
                        uow.tasks.save_task(Task.model_validate(task.model_dump() | settings))
            uow.commit()
    with database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(course.id)
        for unit, settings in unit_settings.items():
            members = [task for task in tasks if task.unit == unit]
            pairs = []
            for task in members:
                version = uow.tasks.latest_version(task.id)
                if version is not None:
                    pairs.append((task, version))
            if settings.get("answer_mode") is AnswerMode.EDITOR:
                blockers = editor_blockers(pairs, course)
                if blockers:
                    raise AdminError(f"units.{unit}: エディタにできません: " + "／".join(blockers))
            if settings.get("file_upload") is False:
                # 動画を受ける課題があると、ファイル選択を止めた瞬間に動画を出す道が無くなる。
                required = file_upload_required(pairs, course)
                if required:
                    raise AdminError(
                        f"units.{unit}: ファイル選択での提出を止められません: "
                        + "／".join(required)
                    )
            for task in members:
                # **検査を通して作り直す**（`model_copy` は検証しない）。
                try:
                    updated = Task.model_validate(task.model_dump() | settings)
                except ValueError as exc:
                    raise AdminError(f"units.{unit}: {exc}") from exc
                uow.tasks.save_task(updated)
        uow.commit()
