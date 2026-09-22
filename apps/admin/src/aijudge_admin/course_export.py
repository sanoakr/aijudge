"""DB のコースを定義ファイルの木へ書き出す／ファイルと DB の差を見る（#363）。

`course_definition` の**逆写像**である。あちらが定義ファイルを読んで課題を
保存し、こちらが保存済みの課題から定義ファイルを組み立てる。

## なぜ要るか

課題の実体は 2 か所にある ── 講義リポジトリ（private）の `assignments/` と、
運用機の DB である。`course apply` は前者から後者への一方通行で、逆が無い。
そのためコンソールで直した問題文・テストケースはファイルに戻らず、
**コンソールでしか作っていない課題は Git のどこにも記録が無い**（実際に
network 2026 の ex3 以降がその状態だった）。

「どちらが正本か」を決めるには、正本の側にすべてが揃っていなければならない。
`export` はその取り込み口であり、`diff` は揃ったあとにずれを見張る側である。

## 書き出しは自分で検算する

逆写像が正しいかどうかは、**順写像に通して確かめられる**。書き出した
`TaskSpec` を `build_task_version` に渡し、DB の版と `substantive` が一致する
ことを 1 件ごとに確かめてから書く。一致しない課題は**書かずに報告する** ──
欠けたまま書くと、それを正本として `apply` し直した日に、課題が静かに
別物になる。「報告できない数値は報告しない」と同じ形である。

さらに書き終えたあと、**書いた木をもう一度読み直して**（`load_course_definition`）
同じ `TaskSpec` になることを確かめる。ファイルの木は読み方に依存する
（`problem_dir` は `in/` `out/` と `*.c` をディレクトリから拾う）ので、
古い年度の残骸が同じディレクトリにあると、書いたものと読めるものが食い違う。

## 出力先

**公開リポジトリには書かない。** 書き出すのは問題文・テストケース・参照解答で、
未公開の回を含む。運用機には aijudge（公開）のチェックアウトもあるので、
`--out` を間違えれば公開リポジトリの作業ツリーに参照解答が載る。
`_refuse_public_checkout` が止める。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from aijudge_authoring import TaskSpec, build_task_version, content, substantive
from aijudge_authoring.importers.sharif_judge import (
    DEFAULT_EVALUATOR,
    REFERENCE_SUFFIXES,
    ImportError_,
    parse_title,
    parse_unit,
)
from aijudge_core import Course, Task, TaskVersion
from aijudge_core.ids import CourseId, TenantId
from aijudge_persistence import Database

from .course_definition import load_course_definition
from .operations import AdminError, course_id_for

__all__ = [
    "CourseDifference",
    "DiffState",
    "ExportedCourse",
    "ExportedTask",
    "SkippedTask",
    "TaskDifference",
    "diff_course",
    "export_course",
]

# 書き出した問題文のファイル名。取り込み器が読む名前と同じ（`sharif_judge`）。
DESC_FILE = "desc.md"
# 参照解答のファイル名。既にある課題ディレクトリに別名の解答があれば、
# そちらの名前を使う（下の `_reference_name`）。
DEFAULT_REFERENCE_STEM = "solution"

# 時刻は**読む人のタイムゾーン**で書く。定義ファイルは人が直すものなので、
# UTC で書くと教員が締切を 9 時間ずらして読む。
#
# 変数の名前と既定は `aijudge_webui.display_zone` と同じものである。
# **import はしない** ── CLI が Web の描画パッケージに依存する形になる。
# 同じ 1 つの環境変数を読む側が 2 つある、という関係にとどめる。
ENV_TIMEZONE = "AIJUDGE_TIMEZONE"
DEFAULT_TIMEZONE = "Asia/Tokyo"

# ここへは書き出さない。**このシステム自身の公開リポジトリ**である
# （運用機にチェックアウトがあるのはこれ）。名前で照合するだけにしてあるのは、
# 公開かどうかを外向きの API に訊くと、回線の都合で書き出しが失敗するため。
PUBLIC_REPOSITORY_NAMES = frozenset({"aijudge"})

_CASE_NAME = re.compile(r"^case(?P<index>\d+)$")


class DiffState(StrEnum):
    """ファイルと DB の食い違い方。"""

    ONLY_FILE = "only_file"
    """定義ファイルにあるが DB に無い（まだ `apply` していない）。"""

    ONLY_DB = "only_db"
    """DB にあるが定義ファイルに無い（コンソールで作って `export` していない）。"""

    CHANGED = "changed"
    """両方にあるが内容が違う。"""


@dataclass(frozen=True)
class TaskDifference:
    key: str
    state: DiffState


@dataclass(frozen=True)
class CourseDifference:
    """`course diff` の結果。

    **コースが無いことと、課題がずれていることを分ける。** 前者は「まだ
    流していない」で、後者は「流したあとにどちらかが動いた」である。
    """

    course_id: CourseId
    course_exists: bool
    differences: tuple[TaskDifference, ...]

    @property
    def in_sync(self) -> bool:
        return self.course_exists and not self.differences


@dataclass(frozen=True)
class SkippedTask:
    """書き出せなかった課題。**黙って落とさない。**"""

    key: str
    reason: str


@dataclass(frozen=True)
class ExportedTask:
    key: str
    files: tuple[Path, ...]


@dataclass(frozen=True)
class ExportedCourse:
    path: Path
    """書き出した `course.yaml`。"""

    tasks: tuple[ExportedTask, ...]
    skipped: tuple[SkippedTask, ...]

    @property
    def complete(self) -> bool:
        return not self.skipped


def _display_zone() -> tzinfo:
    name = os.environ.get(ENV_TIMEZONE, "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_display_zone())


def _repository_name(path: Path) -> str | None:
    """`path` を含む git リポジトリの origin の名前。分からなければ None。"""
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if toplevel.returncode != 0:
            return None
        origin = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # git が無い環境。**通す** ── 判定できないことを禁止の理由にしない。
        return None
    if origin.returncode != 0:
        return None
    url = origin.stdout.strip()
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _refuse_public_checkout(out_dir: Path) -> None:
    """公開リポジトリの作業ツリーへの書き出しを断る。

    **迂回路は作らない。** 正しい出力先は 1 つしかない（講義リポジトリ）。
    `--force` で通せるようにすると、急いでいる日に通る。

    **まだ無いディレクトリでも判定する。** 書き出し先はたいてい存在しない
    （`--out .../assignments` を新しく切る）ので、`git -C` に渡せる最初の
    祖先まで遡って訊く ── ここで諦めると、新しく切るときだけ素通りする。
    """
    probe = out_dir.resolve()
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    name = _repository_name(probe)
    if name is not None and name in PUBLIC_REPOSITORY_NAMES:
        raise AdminError(
            f"{out_dir} は公開リポジトリ {name!r} の作業ツリーの中です。"
            "書き出すのは問題文・テストケース・参照解答で、未公開の回を含みます。"
            "講義リポジトリ（private）の <科目>/<年度>/assignments を指してください"
        )


def _kc_keys(database: Database, version: TaskVersion) -> tuple[str, ...]:
    """Q-matrix の行を正準キーに戻す。

    `q_matrix` が持つのは ID（キーから導いたハッシュ）だけなので、体系を
    引かないとキーに戻せない。引けない ID があれば**書き出しを諦める**
    （下の `_export_spec` が捕まえる）。
    """
    keys: list[str] = []
    with database.unit_of_work() as uow:
        for entry in version.q_matrix:
            kc = uow.skills.get_kc(entry.kc_id)
            if kc is None:
                raise AdminError(f"知識要素 {entry.kc_id} が体系にありません")
            keys.append(kc.key)
    return tuple(keys)


def _test_case_specs(version: TaskVersion, evaluator: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for case in version.test_cases:
        spec: dict[str, Any] = {"name": case.name}
        payload = dict(case.payload)
        if set(payload) == {"input", "expected"}:
            spec["input"] = payload["input"]
            spec["expected"] = payload["expected"]
        else:
            # 入出力の形をしない検証データ（レポートの必須節など・#302）。
            # **空の input/expected を混ぜない。**
            spec["payload"] = payload
        if not case.hidden:
            spec["hidden"] = False
        if case.weight != 1.0:
            spec["weight"] = case.weight
        if case.evaluator_id != evaluator:
            spec["evaluator"] = case.evaluator_id
        cases.append(spec)
    return cases


def _common_fields(
    task: Task, version: TaskVersion, *, course: Course, kc_keys: tuple[str, ...]
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "key": version.source_key,
        "statement": version.statement,
        "unit": task.unit,
        "session": task.session,
        "position": task.position,
        "opens_at": task.opens_at,
        "due_at": task.due_at,
        "max_score": version.max_score,
        "aggregation": version.aggregation,
        "reference_solution": version.reference_solution,
        "knowledge_components": kc_keys,
    }
    if task.accepted_suffixes:
        raw["accepted_suffixes"] = tuple(task.accepted_suffixes)
    if version.subject_profile != course.subject_profile:
        # 課題が自分のプロファイルを持つ場合だけ書く（#195）。既定と同じ値を
        # 書くと、コースのプロファイルを変えたときに課題側が古い値で残る。
        raw["subject_profile"] = version.subject_profile
    return {key: value for key, value in raw.items() if value is not None}


def _candidate_specs(
    task: Task, version: TaskVersion, *, course: Course, kc_keys: tuple[str, ...]
) -> list[TaskSpec]:
    """この版を作りうる宣言の候補。**先に来たものほど読みやすい形。**

    観点が「宣言されたもの」なのか「組み立てられた既定（正しさ＋読みやすさ）」
    なのかは、版だけを見ても決まらない ── どちらでも同じ観点になりうる。
    **候補を作って順写像に通し、一致した方を採る。**
    """
    base = _common_fields(task, version, course=course, kc_keys=kc_keys)
    if task.title != _derived_title(version.statement, str(version.source_key)):
        base["title"] = task.title

    candidates: list[dict[str, Any]] = []

    codes = [criterion.code for criterion in version.criteria]
    if set(codes) <= {"correctness", "readability"} and codes[:1] == ["correctness"]:
        derived = dict(base)
        evaluator = (
            version.criteria[0].evaluator_id if version.test_cases else DEFAULT_EVALUATOR
        ) or DEFAULT_EVALUATOR
        if evaluator != DEFAULT_EVALUATOR:
            derived["evaluator"] = evaluator
        for criterion in version.criteria:
            if criterion.code == "readability":
                derived["readability_weight"] = criterion.weight
        if version.test_cases:
            derived["test_cases"] = _test_case_specs(version, evaluator)
        candidates.append(derived)

    declared = dict(base)
    declared["criteria"] = [
        {
            "code": criterion.code,
            "title": criterion.title,
            "description": criterion.description,
            "weight": criterion.weight,
            **({} if criterion.evaluator_id is None else {"evaluator": criterion.evaluator_id}),
            "levels": [
                {
                    "level": level.level,
                    "label": level.label,
                    "descriptor": level.descriptor,
                    "score_ratio": level.score_ratio,
                }
                for level in criterion.levels
            ],
        }
        for criterion in version.criteria
    ]
    if version.test_cases:
        declared["test_cases"] = _test_case_specs(version, DEFAULT_EVALUATOR)
    candidates.append(declared)

    specs: list[TaskSpec] = []
    for raw in candidates:
        try:
            specs.append(TaskSpec.model_validate(raw))
        except ValueError:
            # 候補が宣言として成立しないのは普通に起きる（観点を宣言する課題に
            # `readability_weight` は書けない、など）。**次の候補を試す。**
            continue
    return specs


def _derived_title(statement: str, key: str) -> str:
    """`save_task` が題名を書かなかったときに使う値（`authoring._title_of`）。"""
    try:
        title, _tag = parse_title(statement)
    except ImportError_:
        return key
    return title


def _faithful(spec: TaskSpec, version: TaskVersion, *, course: Course) -> bool:
    """この宣言から、同じ版がもう一度組み立たるか。"""
    rebuilt = build_task_version(
        spec,
        course_id=course.id,
        subject_profile=spec.subject_profile or course.subject_profile,
        authored_by=version.provenance.authored_by,  # type: ignore[arg-type]
        version=version.version,
        generated_by=version.provenance.generated_by,
        generation_prompt_version=version.provenance.generation_prompt_version,
    )
    return substantive(rebuilt) == substantive(version)


def _export_spec(
    database: Database, task: Task, version: TaskVersion, *, course: Course
) -> TaskSpec:
    if version.source_key is None:
        # 古い版には入っていない（`TaskVersion.source_key`）。鍵が無いと
        # 課題 ID も観点 ID も導けないので、宣言に戻せない。
        raise AdminError("この版には source_key がありません（古い取り込み）")
    kc_keys = _kc_keys(database, version)
    for spec in _candidate_specs(task, version, course=course, kc_keys=kc_keys):
        if _faithful(spec, version, course=course):
            return spec
    raise AdminError(
        "宣言に戻すと内容が変わります"
        "（宣言の形に無い値を持つ版です。手書き答案の設定などが該当します）"
    )


def _reference_name(task_dir: Path, spec: TaskSpec, course: Course) -> str | None:
    """参照解答を書くファイル名。拾い直せない形なら None（YAML に直接書く）。

    **既にある名前を使う。** `find_reference_solution` は拡張子ごとに
    `sorted(glob)` の先頭を採るので、`hello.c` がある所へ `solution.c` を
    足すと、読み直したときに拾われるのは `hello.c` のままになる。
    """
    existing = [
        path.name for suffix in REFERENCE_SUFFIXES for path in sorted(task_dir.glob(f"*{suffix}"))
    ]
    if existing:
        return existing[0]
    suffixes = tuple(spec.accepted_suffixes) or tuple(course.upload_suffixes)
    for suffix in REFERENCE_SUFFIXES:
        if suffix in suffixes:
            return f"{DEFAULT_REFERENCE_STEM}{suffix}"
    return None


def _plain_cases(spec: TaskSpec) -> bool:
    """`in/` `out/` の形に書き戻せるテストケースか。

    書き戻せるのは、取り込み器が作るのと同じ形のときだけ ── `case1..caseN` が
    順に並び、入出力を持ち、隠されていて、重みが 1.0 で、評価器が課題の既定。
    1 つでも外れたら **YAML に明示的に書く**（`_from_problem_dir` は YAML が
    書いていればディレクトリを読まない）。
    """
    for index, case in enumerate(spec.test_cases, start=1):
        match = _CASE_NAME.match(case.name)
        if match is None or int(match.group("index")) != index:
            return False
        if case.payload or not case.hidden or case.weight != 1.0 or case.evaluator is not None:
            return False
    return bool(spec.test_cases)


def _write_task(
    out_dir: Path, spec: TaskSpec, *, course: Course
) -> tuple[dict[str, Any], list[Path]]:
    """課題 1 件を書き、`tasks:` に載せる 1 項目を返す。"""
    task_dir = out_dir / spec.key
    task_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    desc = task_dir / DESC_FILE
    desc.write_text(spec.statement, encoding="utf-8")
    written.append(desc)

    entry: dict[str, Any] = {}
    unit, session, position = parse_unit(task_dir)
    from_dir = unit == spec.unit and session == spec.session and position == spec.position
    if from_dir and "/" in spec.key:
        # ディレクトリから鍵・回・順序が決まる形（Sharif Judge 形式）。
        entry["problem_dir"] = spec.key
    else:
        entry["statement_file"] = f"{spec.key}/{DESC_FILE}"
        entry["key"] = spec.key
        for field in ("unit", "session", "position"):
            value = getattr(spec, field)
            if value is not None:
                entry[field] = value

    # **古い形を消してから書く。** 残っていると読み直しで拾われる。
    for stale in ("in", "out"):
        shutil.rmtree(task_dir / stale, ignore_errors=True)

    if _plain_cases(spec):
        for index, case in enumerate(spec.test_cases, start=1):
            for sub, name, payload in (
                ("in", f"input{index}.txt", case.input),
                ("out", f"output{index}.txt", case.expected),
            ):
                path = task_dir / sub / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
                written.append(path)
    elif spec.test_cases:
        entry["test_cases"] = _test_case_specs_from(spec)

    if spec.reference_solution is not None:
        name = _reference_name(task_dir, spec, course)
        if name is None:
            entry["reference_solution"] = spec.reference_solution
        else:
            path = task_dir / name
            path.write_text(spec.reference_solution, encoding="utf-8")
            written.append(path)

    if spec.title is not None:
        entry["title"] = spec.title
    if spec.accepted_suffixes:
        entry["accepted_suffixes"] = list(spec.accepted_suffixes)
    if spec.max_score != 100.0:
        entry["max_score"] = spec.max_score
    if spec.evaluator != DEFAULT_EVALUATOR:
        entry["evaluator"] = spec.evaluator
    if spec.readability_weight:
        entry["readability_weight"] = spec.readability_weight
    if spec.aggregation is not None:
        entry["aggregation"] = spec.aggregation.value
    if spec.subject_profile is not None:
        entry["subject_profile"] = spec.subject_profile
    if spec.knowledge_components:
        entry["knowledge_components"] = list(spec.knowledge_components)
    if spec.criteria:
        entry["criteria"] = [criterion.model_dump(exclude_none=True) for criterion in spec.criteria]
    return entry, written


def _test_case_specs_from(spec: TaskSpec) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for case in spec.test_cases:
        row: dict[str, Any] = {"name": case.name}
        if case.payload:
            row["payload"] = dict(case.payload)
        else:
            row["input"] = case.input
            row["expected"] = case.expected
        if not case.hidden:
            row["hidden"] = False
        if case.weight != 1.0:
            row["weight"] = case.weight
        if case.evaluator is not None:
            row["evaluator"] = case.evaluator
        cases.append(row)
    return cases


def _unit_schedules(specs: tuple[TaskSpec, ...]) -> dict[str, dict[str, datetime]]:
    """回ごとに日程が揃っていれば `units:` にまとめる。

    日程は問題セット単位で決まる（`aijudge_core.task.Task`）ので、揃っている
    のが普通である。課題ごとに 2 行ずつ書くと、定義ファイルは日付の羅列になる。
    """
    by_unit: dict[str, list[TaskSpec]] = {}
    for spec in specs:
        if spec.unit:
            by_unit.setdefault(spec.unit, []).append(spec)
    schedules: dict[str, dict[str, datetime]] = {}
    for unit, members in by_unit.items():
        schedule: dict[str, datetime] = {}
        for field in ("opens_at", "due_at"):
            values = {getattr(spec, field) for spec in members}
            if len(values) == 1:
                value = values.pop()
                if value is not None:
                    schedule[field] = value
        if schedule:
            schedules[unit] = schedule
    return schedules


def export_course(
    database: Database,
    *,
    course_id: CourseId,
    out_dir: Path,
    force: bool = False,
) -> ExportedCourse:
    """コースを定義ファイルの木へ書き出す。**読み出しだけ**（DB は変えない）。"""
    _refuse_public_checkout(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise AdminError(f"{out_dir} は空ではありません（上書きするなら --force）")

    with database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
        if course is None:
            raise AdminError(f"コースがありません: {course_id}")
        tasks = uow.tasks.list_for_course(course_id)
        versions = {task.id: uow.tasks.latest_version(task.id) for task in tasks}

    exported: list[ExportedTask] = []
    skipped: list[SkippedTask] = []
    specs: list[TaskSpec] = []
    entries: list[dict[str, Any]] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    for task in sorted(tasks, key=lambda item: (item.session or 10**6, item.position or 10**6)):
        version = versions[task.id]
        key = version.source_key if version is not None else task.title
        if version is None:
            skipped.append(SkippedTask(key=str(key), reason="版がありません"))
            continue
        try:
            spec = _export_spec(database, task, version, course=course)
        except AdminError as exc:
            skipped.append(SkippedTask(key=str(key), reason=str(exc)))
            continue
        entry, files = _write_task(out_dir, spec, course=course)
        specs.append(spec)
        entries.append(entry)
        exported.append(ExportedTask(key=spec.key, files=tuple(files)))

    schedules = _unit_schedules(tuple(specs))
    for entry, spec in zip(entries, specs, strict=True):
        # 回にまとめた日程は課題側から落とす（**既定と同じ値を二重に書かない**）。
        schedule = schedules.get(spec.unit or "", {})
        for field in ("opens_at", "due_at"):
            value = getattr(spec, field)
            if value is None:
                continue
            if schedule.get(field) != value:
                entry[field] = _local(value)

    document: dict[str, Any] = {
        "course": {
            "code": course.code,
            "title": course.title,
            "term": course.term,
            "subject_profile": course.subject_profile,
        },
    }
    if course.description:
        document["course"]["description"] = course.description
    if course.upload_suffixes:
        document["course"]["upload_suffixes"] = list(course.upload_suffixes)
    if course.knowledge_components:
        document["course"]["knowledge_components"] = list(course.knowledge_components)
    if schedules:
        document["units"] = {
            unit: {field: _local(value) for field, value in schedule.items()}
            for unit, schedule in schedules.items()
        }
    document["tasks"] = entries

    path = out_dir / "course.yaml"
    path.write_text(_HEADER + _dump(document), encoding="utf-8")

    _verify_written_tree(path, specs)
    return ExportedCourse(path=path, tasks=tuple(exported), skipped=tuple(skipped))


def _verify_written_tree(path: Path, specs: list[TaskSpec]) -> None:
    """書いた木を読み直して、同じ宣言になることを確かめる。

    ファイルの木は読み方に依存する（`problem_dir` は `in/` `out/` と `*.c` を
    ディレクトリから拾う）ので、**書けたことは読めることを意味しない。**
    """
    reloaded = {spec.key: spec for spec in load_course_definition(path).tasks}
    for spec in specs:
        again = reloaded.get(spec.key)
        if again is None:
            raise AdminError(f"書き出した {spec.key} を読み直せません（{path}）")
        if again != spec:
            raise AdminError(
                f"書き出した {spec.key} が読み直すと変わります（{path}）。"
                "同じディレクトリに別年度の残骸がある可能性があります"
            )


def diff_course(database: Database, path: Path, *, tenant_id: TenantId) -> CourseDifference:
    """定義ファイルと DB の差を見る。**何も書かない。**

    判定は `save_task` と同じ規則（`content` の比較）に揃える ── 別の規則で
    数えると、「一致」と出したものが `apply` で版を上げる。
    """
    definition = load_course_definition(path)
    spec = definition.course
    course_id = course_id_for(tenant_id, spec["code"], spec["term"])

    with database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
        if course is None:
            return CourseDifference(course_id=course_id, course_exists=False, differences=())
        stored = {
            version.source_key: version
            for task in uow.tasks.list_for_course(course_id)
            if (version := uow.tasks.latest_version(task.id)) is not None
        }

    differences: list[TaskDifference] = []
    for task in definition.tasks:
        latest = stored.pop(task.key, None)
        if latest is None:
            differences.append(TaskDifference(key=task.key, state=DiffState.ONLY_FILE))
            continue
        candidate = build_task_version(
            task,
            course_id=course_id,
            subject_profile=task.subject_profile or course.subject_profile,
            authored_by=latest.provenance.authored_by,  # type: ignore[arg-type]
            generated_by=latest.provenance.generated_by,
            generation_prompt_version=latest.provenance.generation_prompt_version,
        )
        if content(candidate) != content(latest):
            differences.append(TaskDifference(key=task.key, state=DiffState.CHANGED))

    for key in stored:
        differences.append(TaskDifference(key=str(key), state=DiffState.ONLY_DB))

    return CourseDifference(
        course_id=course_id,
        course_exists=True,
        differences=tuple(sorted(differences, key=lambda item: item.key)),
    )


_HEADER = """\
# このファイルは `aijudge-admin course export` が書き出した。
#
# 正本は講義リポジトリ（private）のこのディレクトリである。コンソールで
# 課題を直したら export し直して PR を出す。ずれていないかは
# `aijudge-admin course diff --file <このファイル>` で確かめる。
#
# 形式は aiJudge の apps/admin/src/aijudge_admin/course_definition.py 冒頭。
"""


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """複数行の文字列はブロック記法で書く。

    問題文は Markdown で、引用符付き 1 行に潰れると人には読めない
    ── 定義ファイルは教員が直すものである。
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, _str_presenter)
_Dumper.add_representer(
    datetime,
    lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", data.isoformat()),
)


def _dump(document: dict[str, Any]) -> str:
    return yaml.dump(
        document,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
