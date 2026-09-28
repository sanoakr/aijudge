"""提出どうしの類似を Dolos で調べ、報告をファイルに残す（#203・ADR 0029）。

**判定はしない ── 似ている組を担当教員に示すだけである（P5）。** 同じ課題を
解けば似る。入門課題は正解がほぼ 1 つしかない。「盗用」という語を機械に言わせない。

- **提出物を外に出さない（P7）。** Dolos を digest で固定したイメージのまま、
  提出物と同じ sandbox（gVisor・ネットワークなし・nobody）で動かす。ホスト
  された dolos.ugent.be へ送る経路はここに無い。Dolos のサーバも立てない ──
  自前のサーバは検査ごとにコンテナを起動するため docker.sock を要する
- **DB に何も書かない。** 回ごとに報告の CSV と `run.json` を
  `AIJUDGE_SIMILARITY_DIR/<コース>/<課題>/<回>/` に置く。入口のページも
  後始末もこの `run.json` を読む。課題ごとに最新の 1 回だけを残す
- **報告は全員のコード全文の写しである**（`files.csv`）。提出と同じだけしか残さない
  （提出が消えたら消す・`sweep_orphans`）。バックアップしない
- **測れなかったら `NOT_MEASURED` と理由を残す。** 「似た組が 0」と区別する

運用機で実測した制限（2026-09-29・gVisor）: プロセス数は 64 が要る（32 で分析が
落ち、24 以下ではコンテナが立たない）。メモリ 512 MiB で C 300 件が通り、256 MiB
では落ちた。時間は件数の 2 乗に近い（C 60 件 6 秒・300 件 102 秒、CPU 1 つ）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import Artifact, Submission, Task
from aijudge_core.ids import CourseId, SubmissionId, TaskId
from aijudge_sandbox import ExecRequest, Limits, Sandbox, SandboxError, build_tool_sandbox
from aijudge_submission import ArtifactStore
from aijudge_unit_of_work import Store, StoreUnitOfWork

logger = logging.getLogger(__name__)

__all__ = [
    "DOLOS_IMAGE",
    "ENV_SIMILARITY_DIR",
    "ENV_SIMILARITY_IMAGE",
    "REPORT_FILES",
    "NotMeasuredReason",
    "SimilarityRun",
    "latest_run",
    "list_runs",
    "run_for_task",
    "sweep_orphans",
]

#: Dolos の CLI。**digest で固定する** ── タグは付け替えられる。画面の配布物も
#: 同じイメージから取り出すので、CLI と画面の版は必ずそろう。
DOLOS_IMAGE = (
    "ghcr.io/dodona-edu/dolos-cli@sha256:"
    "39ca828cb51a0acbe7bda18bdcfe752fac29a38f5db9dc6d91f22ba6d25e6f38"
)
DOLOS_VERSION = "2.9.3"
ENV_SIMILARITY_DIR = "AIJUDGE_SIMILARITY_DIR"
ENV_SIMILARITY_IMAGE = "AIJUDGE_SIMILARITY_IMAGE"

#: 報告のファイル。画面（Dolos の web）が `data/` の下から読む 4 つだけを残す。
REPORT_FILES = ("metadata.csv", "pairs.csv", "kgrams.csv", "files.csv")
RUN_FILE = "run.json"
DATA_DIR = "data"
RUN_SCHEMA_VERSION = 1

#: 拡張子から Dolos の言語へ。**採点の言語設定は読まない** ── 課題ごとの上書き・
#: コースの上書き・雛形の 3 段を引き直すより、提出されたファイルそのものが確か。
LANGUAGE_BY_SUFFIX = {".c": "c", ".h": "c", ".py": "python"}

#: 比べる相手がいないと組ができない。
MIN_SUBMISSIONS = 2
#: 1 回で扱う上限。512 MiB で 300 件が通ったところまで（実測）。
MAX_SUBMISSIONS = 300

#: 実測に合わせた上限。プロセス数 64 とメモリ 512 MiB は gVisor で要る最小に近い。
LIMITS = Limits(
    cpu_seconds=600,
    wall_seconds=600.0,
    memory_bytes=512 * 1024 * 1024,
    processes=64,
    # 報告は入力の約 40 倍（C 300 件で 55 MB）。1 ファイルの上限（`--ulimit=fsize`）に効く。
    output_bytes=256 * 1024 * 1024,
    workspace_bytes=1024 * 1024 * 1024,
)

#: Dolos に渡す引数。**記録に残す**（同じ入力でも引数で結果が変わる）。
#: `-M`（多くの提出に出る断片を無視）は実データを見て決める ── いまは既定のまま。
DOLOS_PARAMS: tuple[str, ...] = ()


class NotMeasuredReason(StrEnum):
    """測れなかった理由。**再試行してよいか**で 2 つに分かれる（`retryable`）。"""

    TOO_FEW = "too_few"
    TOO_MANY = "too_many"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    MIXED_LANGUAGES = "mixed_languages"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    TOOL_FAILED = "tool_failed"

    @property
    def retryable(self) -> bool:
        """入力が同じでも、次に回せば測れうるか（環境の不調）。"""
        return self in (NotMeasuredReason.SANDBOX_UNAVAILABLE, NotMeasuredReason.TOOL_FAILED)


class RunSubmission(BaseModel):
    """報告に入れた提出 1 件と、画面に出る仮の名前。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    submission_id: SubmissionId
    pseudonym: str
    filename: str
    content_hash: str


class SimilarityRun(BaseModel):
    """`run.json` の中身。**入口のページ・後始末・自動実行がこれだけを読む。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = RUN_SCHEMA_VERSION
    run_id: str
    course_id: CourseId
    task_id: TaskId
    unit: str | None = None
    task_title: str = ""
    created_at: datetime
    image: str
    tool_version: str = DOLOS_VERSION
    params: tuple[str, ...] = ()
    language: str | None = None
    input_hash: str
    submissions: tuple[RunSubmission, ...] = ()
    not_measured: NotMeasuredReason | None = None
    detail: str = ""
    sandbox: str | None = None
    isolation: str | None = None
    duration_ms: int = Field(default=0, ge=0)

    @property
    def measured(self) -> bool:
        return self.not_measured is None


@dataclass(frozen=True)
class _Entry:
    submission: Submission
    filename: str
    content: bytes
    content_hash: str
    language: str | None


def _latest_per_learner(submissions: Iterable[Submission]) -> list[Submission]:
    """学習者 1 人につき最新の提出 1 件。**成績に数える提出だけ**（教員・TA・デモは除く）。

    再提出を全部入れると、自分自身との組が上位を埋める。
    """
    latest: dict[str, tuple[datetime, int, Submission]] = {}
    for submission in submissions:
        if submission.is_trial or submission.submitted_at is None:
            continue
        key = (submission.submitted_at, submission.attempt, submission)
        current = latest.get(submission.learner_id)
        if current is None or key[:2] > current[:2]:
            latest[submission.learner_id] = key
    return sorted((item[2] for item in latest.values()), key=lambda s: str(s.id))


def _code_artifact(submission: Submission) -> Artifact | None:
    """採点に使うコードの成果物（最初の 1 つ）。消されたものは使わない。"""
    for artifact in submission.gradable_artifacts:
        if artifact.kind.value == "code" and not artifact.is_purged:
            return artifact
    return None


def _entries(submissions: Sequence[Submission], store: ArtifactStore) -> list[_Entry]:
    entries: list[_Entry] = []
    for submission in submissions:
        artifact = _code_artifact(submission)
        if artifact is None:
            continue
        suffix = PurePosixPath(artifact.filename or "").suffix.lower()
        entries.append(
            _Entry(
                submission=submission,
                filename=artifact.filename or "",
                content=store.get(artifact.storage_key),
                content_hash=artifact.content_hash,
                language=LANGUAGE_BY_SUFFIX.get(suffix),
            )
        )
    return entries


def input_hash(entries: Sequence[_Entry], *, image: str, params: Sequence[str]) -> str:
    """入力の指紋。**同じなら回し直さない**（自動実行の判定）。"""
    digest = hashlib.sha256()
    digest.update(image.encode())
    digest.update("\0".join(params).encode())
    for entry in sorted(entries, key=lambda e: str(e.submission.id)):
        digest.update(f"\0{entry.submission.id}\0{entry.content_hash}".encode())
    return digest.hexdigest()


def build_dataset(entries: Sequence[_Entry], language: str) -> tuple[dict[str, bytes], str]:
    """Dolos に渡すファイルと `info.csv`。**名前は仮のもの**（`S-001` など）にする。

    `info.csv` の `full_name`・`labels`・`created_at` はそのまま画面に出る。実名や
    学籍番号を入れない ── 仮の名前と提出の対応は `run.json` に残し、コンソールが引く。
    """
    suffix = {"c": ".c", "python": ".py"}[language]
    files: dict[str, bytes] = {}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["filename", "id", "full_name", "labels", "created_at"])
    for index, entry in enumerate(entries, start=1):
        pseudonym = f"S-{index:03d}"
        name = f"{pseudonym}{suffix}"
        files[name] = entry.content
        submitted = entry.submission.submitted_at or entry.submission.created_at
        writer.writerow(
            [
                name,
                str(index),
                pseudonym,
                "",
                submitted.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000"),
            ]
        )
    return files, out.getvalue()


def similarity_root() -> Path | None:
    raw = os.environ.get(ENV_SIMILARITY_DIR, "").strip()
    return Path(raw).expanduser() if raw else None


def task_dir(root: Path, course_id: CourseId, task_id: TaskId) -> Path:
    return root / str(course_id) / str(task_id)


def _write_run(target: Path, run: SimilarityRun) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / RUN_FILE).write_text(run.model_dump_json(indent=2), encoding="utf-8")


def read_run(run_dir: Path) -> SimilarityRun | None:
    """壊れた・古い形式の `run.json` は無いものとして扱う（入口のページを落とさない）。"""
    try:
        return SimilarityRun.model_validate_json((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_runs(root: Path, course_id: CourseId) -> list[SimilarityRun]:
    """このコースの回（課題ごとに最新の 1 回）。"""
    runs: list[SimilarityRun] = []
    course = root / str(course_id)
    if not course.is_dir():
        return runs
    for task in sorted(p for p in course.iterdir() if p.is_dir()):
        run = latest_run(root, course_id, TaskId(task.name))
        if run is not None:
            runs.append(run)
    return runs


def latest_run(root: Path, course_id: CourseId, task_id: TaskId) -> SimilarityRun | None:
    directory = task_dir(root, course_id, task_id)
    if not directory.is_dir():
        return None
    runs = [run for p in directory.iterdir() if p.is_dir() and (run := read_run(p)) is not None]
    return max(runs, key=lambda r: r.created_at, default=None)


def run_dir(root: Path, run: SimilarityRun) -> Path:
    return task_dir(root, run.course_id, run.task_id) / run.run_id


def _keep_only(root: Path, run: SimilarityRun) -> None:
    """課題ごとに最新の 1 回だけ残す。**写しを増やさない。**"""
    directory = task_dir(root, run.course_id, run.task_id)
    for child in directory.iterdir():
        if child.is_dir() and child.name != run.run_id:
            shutil.rmtree(child, ignore_errors=True)


SandboxFactory = Callable[[str], Sandbox]


def run_for_task(
    database: Store,
    task_id: TaskId,
    *,
    artifact_store: ArtifactStore,
    root: Path,
    now: datetime | None = None,
    image: str | None = None,
    sandbox_factory: SandboxFactory = build_tool_sandbox,
    force: bool = False,
) -> SimilarityRun | None:
    """1 課題を調べて報告を残す。**入力が前回と同じなら何もしない**（`None`）。

    前回が環境の不調で測れなかった（`retryable`）なら、入力が同じでも回し直す。
    `force` は手で回すとき用。
    """
    at = now or datetime.now(UTC)
    tool = image or os.environ.get(ENV_SIMILARITY_IMAGE, "").strip() or DOLOS_IMAGE
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        if task is None:
            raise LookupError(f"課題 {task_id!r} がありません")
        versions = [version.id for version in uow.tasks.list_versions(task_id)]
        submissions = _latest_per_learner(uow.submissions.list_for_versions(versions))
    entries = _entries(submissions, artifact_store)
    fingerprint = input_hash(entries, image=tool, params=DOLOS_PARAMS)

    previous = latest_run(root, task.course_id, task.id)
    if (
        not force
        and previous is not None
        and previous.input_hash == fingerprint
        and not (previous.not_measured and previous.not_measured.retryable)
    ):
        return None

    run = _measure(task, entries, fingerprint, tool=tool, at=at, root=root, factory=sandbox_factory)
    _keep_only(root, run)
    return run


def _run_id(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _base(task: Task, fingerprint: str, *, tool: str, at: datetime) -> dict[str, object]:
    return {
        "run_id": _run_id(at),
        "course_id": task.course_id,
        "task_id": task.id,
        "unit": task.unit,
        "task_title": task.title,
        "created_at": at,
        "image": tool,
        "params": DOLOS_PARAMS,
        "input_hash": fingerprint,
    }


def _not_measured(
    task: Task,
    fingerprint: str,
    reason: NotMeasuredReason,
    detail: str,
    *,
    tool: str,
    at: datetime,
    root: Path,
    **extra: object,
) -> SimilarityRun:
    run = SimilarityRun.model_validate(
        {**_base(task, fingerprint, tool=tool, at=at), "not_measured": reason, "detail": detail}
        | extra
    )
    _write_run(task_dir(root, task.course_id, task.id) / run.run_id, run)
    logger.info("類似の検査を測れませんでした: %s %s（%s）", task.id, reason.value, detail)
    return run


def _measure(
    task: Task,
    entries: Sequence[_Entry],
    fingerprint: str,
    *,
    tool: str,
    at: datetime,
    root: Path,
    factory: SandboxFactory,
) -> SimilarityRun:
    def refuse(reason: NotMeasuredReason, detail: str, **extra: object) -> SimilarityRun:
        return _not_measured(
            task, fingerprint, reason, detail, tool=tool, at=at, root=root, **extra
        )

    if len(entries) < MIN_SUBMISSIONS:
        return refuse(NotMeasuredReason.TOO_FEW, f"コードの提出が {len(entries)} 件です")
    if len(entries) > MAX_SUBMISSIONS:
        return refuse(
            NotMeasuredReason.TOO_MANY,
            f"コードの提出が {len(entries)} 件で、1 回の上限（{MAX_SUBMISSIONS} 件）を超えます",
        )
    languages = {entry.language for entry in entries}
    if None in languages:
        return refuse(
            NotMeasuredReason.UNSUPPORTED_LANGUAGE,
            "C（.c）・Python（.py）以外のファイルが含まれます",
        )
    if len(languages) > 1:
        return refuse(
            NotMeasuredReason.MIXED_LANGUAGES,
            f"言語が混ざっています: {sorted(str(lang) for lang in languages)}",
        )
    language = str(languages.pop())
    files, info = build_dataset(entries, language)
    submissions = tuple(
        RunSubmission(
            submission_id=entry.submission.id,
            pseudonym=f"S-{index:03d}",
            filename=entry.filename,
            content_hash=entry.content_hash,
        )
        for index, entry in enumerate(entries, start=1)
    )

    try:
        sandbox = factory(tool)
    except SandboxError as exc:
        return refuse(NotMeasuredReason.SANDBOX_UNAVAILABLE, str(exc), language=language)

    started = time.monotonic()
    target = task_dir(root, task.course_id, task.id) / _run_id(at)
    with sandbox.workspace() as workspace:
        for name, content in files.items():
            workspace.write(f"ds/{name}", content)
        workspace.write("ds/info.csv", info)
        result = workspace.run(
            ExecRequest(
                argv=(
                    "dolos",
                    "run",
                    "-l",
                    language,
                    "-f",
                    "csv",
                    "-o",
                    "out",
                    *DOLOS_PARAMS,
                    "ds/info.csv",
                ),
                limits=LIMITS,
            )
        )
        duration = int((time.monotonic() - started) * 1000)
        report = workspace.path / "out"
        missing = [name for name in REPORT_FILES if not (report / name).is_file()]
        if not result.ok or missing:
            detail = (
                f"exit={result.exit_code} timed_out={result.timed_out} "
                f"signal={result.signal_name} missing={missing} "
                f"stderr={result.stderr.strip()[-300:]}"
            )
            return refuse(
                NotMeasuredReason.TOOL_FAILED,
                detail,
                language=language,
                sandbox=sandbox.name,
                isolation=sandbox.isolation.value,
                duration_ms=duration,
            )
        # **決まった 4 つの通常ファイルだけを写す。** 道具が作った他のものは持ち出さない。
        data = target / DATA_DIR
        data.mkdir(parents=True, exist_ok=True)
        for name in REPORT_FILES:
            source = report / name
            if source.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
                return refuse(NotMeasuredReason.TOOL_FAILED, f"{name} がリンクです")
            shutil.copyfile(source, data / name)

    run = SimilarityRun.model_validate(
        _base(task, fingerprint, tool=tool, at=at)
        | {
            "language": language,
            "submissions": submissions,
            "sandbox": sandbox.name,
            "isolation": sandbox.isolation.value,
            "duration_ms": duration,
        }
    )
    _write_run(target, run)
    logger.info(
        "類似の検査: %s %d 件 %.1f 秒（%s）",
        task.id,
        len(entries),
        duration / 1000,
        sandbox.name,
    )
    return run


def sweep_orphans(database: Store, root: Path) -> list[Path]:
    """**提出と一緒に消す**（#203 の決定 4）。消した回のディレクトリを返す。

    消すのは次のどれかに当たる回:
    - コースか課題がもう無い（コースの削除・課題の移動）
    - 入れた提出のどれかが無い、またはそのコードの実体が消された（purge）

    報告は全員のコードの写しなので、1 件でも元が消えたら丸ごと消す。自動実行の
    次の周回で、残っている提出だけで作り直される。
    """
    removed: list[Path] = []
    if not root.is_dir():
        return removed
    with database.unit_of_work() as uow:
        for course_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if uow.identity.get_course(CourseId(course_dir.name)) is None:
                shutil.rmtree(course_dir, ignore_errors=True)
                removed.append(course_dir)
                continue
            for directory in sorted(p for p in course_dir.iterdir() if p.is_dir()):
                task = uow.tasks.get_task(TaskId(directory.name))
                if task is None or str(task.course_id) != course_dir.name:
                    shutil.rmtree(directory, ignore_errors=True)
                    removed.append(directory)
                    continue
                for child in sorted(p for p in directory.iterdir() if p.is_dir()):
                    run = read_run(child)
                    if run is None or not _sources_remain(uow, run):
                        shutil.rmtree(child, ignore_errors=True)
                        removed.append(child)
    return removed


def _sources_remain(uow: StoreUnitOfWork, run: SimilarityRun) -> bool:
    for item in run.submissions:
        submission = uow.submissions.get(item.submission_id)
        if submission is None:
            return False
        artifact = _code_artifact(submission)
        if artifact is None or artifact.content_hash != item.content_hash:
            return False
    return True


def run_payload(run: SimilarityRun) -> str:
    """ログ・CLI 用の短い要約。"""
    return json.dumps(
        {
            "task": str(run.task_id),
            "measured": run.measured,
            "reason": run.not_measured.value if run.not_measured else None,
            "submissions": len(run.submissions),
            "seconds": round(run.duration_ms / 1000, 1),
        },
        ensure_ascii=False,
    )
