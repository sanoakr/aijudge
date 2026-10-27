"""提出を、**指定した相手**（他の課題・取り込んだ過年度の束）と比べる（#653）。

過年度のレポートを写して出していないかを見るため。相手は課題の `similarity_references`
で指定する（`aijudge_core.SimilarityReference`）。

- **この課題の提出 × 相手の文書だけを比べる。** 相手どうしは比べない。同じ課題の提出どうしは
  ふだんの回（`code_similarity.run_for_task`）が見ている
- 比べ方は「テキストとして」（Dolos `char`）と「内容として」（埋め込み）。コードとしては
  比べない ── 過年度の写しはレポートを想定している
- **相手の側は出どころ（`ReferenceDocument.label`）と類似度しか残さない。** 名前も本文も
  画面に出さない（2026-10-28 の決定）。Dolos の報告は `pairs.csv` だけを持ち出す ──
  `files.csv` には相手の本文が入る
- 束の埋め込みはモデルごとに保存して使い回す（毎周回送り直さない）
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import shutil
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from aijudge_core import Task, derived_id
from aijudge_core.ids import TaskId
from aijudge_sandbox import ExecRequest, SandboxError, build_tool_sandbox
from aijudge_submission import ArtifactStore
from aijudge_unit_of_work import Store

from .code_similarity import (
    CHAR_LANGUAGE,
    DATA_DIR,
    DOLOS_IMAGE,
    ENV_SIMILARITY_IMAGE,
    LIMITS,
    MAX_SUBMISSIONS,
    TEXT_PARAMS,
    NotMeasuredReason,
    ReferenceDocument,
    RunSubmission,
    SandboxFactory,
    SimilarityMode,
    SimilarityRun,
    _base,
    _entries,
    _Entry,
    _keep_only,
    _latest_per_learner,
    _readable,
    _run_id,
    _write_run,
    input_hash,
    latest_run,
    load_submissions,
    task_dir,
)
from .content_similarity import ContentSettings, _compare, _embed, paragraphs
from .operations import course_id_for
from .similarity_archive import archive_root, load_archive

logger = logging.getLogger(__name__)

#: 相手と比べる比べ方。
REFERENCE_MODES = (SimilarityMode.TEXT, SimilarityMode.CONTENT)
EMBEDDING_CACHE = "embeddings"


@dataclass(frozen=True)
class _Reference:
    document: ReferenceDocument
    text: str


def resolve(
    database: Store,
    task: Task,
    mode: SimilarityMode,
    *,
    artifact_store: ArtifactStore,
    root: Path,
) -> tuple[list[_Reference], list[str]]:
    """相手の文書と、見つからなかった相手の説明。**相手の名前・本文は画面に出さない。**"""
    found: list[tuple[ReferenceDocument, str]] = []
    missing: list[str] = []
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(task.course_id)
        if course is None:
            return [], ["コースがありません"]
        for reference in task.similarity_references:
            if reference.archive is not None:
                loaded = load_archive(root, reference.archive)
                if loaded is None:
                    missing.append(f"束 {reference.archive}")
                    continue
                manifest, documents = loaded
                for item in documents:
                    found.append(
                        (
                            ReferenceDocument(
                                pseudonym="",
                                label=f"{manifest.label} の {item.document.id}",
                                content_hash=item.document.content_hash,
                                archive=manifest.name,
                                document=item.document.id,
                            ),
                            item.text,
                        )
                    )
                continue
            assert reference.task is not None
            other_id = (
                course.id
                if reference.course is None
                else course_id_for(course.tenant_id, reference.course, reference.term or "")
            )
            target_id = TaskId(derived_id("tsk", str(other_id), reference.task))
            other_course = uow.identity.get_course(other_id)
            target = uow.tasks.get_task(target_id)
            if other_course is None or target is None or target.id == task.id:
                missing.append(
                    f"課題 {reference.course or course.code} {reference.term or course.term} "
                    f"{reference.task}"
                )
                continue
            versions = [version.id for version in uow.tasks.list_versions(target.id)]
            submissions = _latest_per_learner(uow.submissions.list_for_versions(versions))
            source = f"{other_course.title}（{other_course.term}）{target.title}"
            for entry in _entries(submissions, mode):
                found.append(
                    (
                        ReferenceDocument(
                            pseudonym="",
                            label=f"{source} の提出",
                            content_hash=entry.content_hash,
                            submission_id=entry.submission.id,
                        ),
                        "",
                    )
                )
    # 他の課題の提出は、ここで本文を読む（PDF は採点と同じ抽出）。
    references: list[_Reference] = []
    for document, text in found:
        if document.submission_id is not None:
            text = _submission_text(database, document, mode, artifact_store) or ""
            if not text:
                continue
        pseudonym = f"R-{len(references) + 1:03d}"
        label = document.label if document.archive else f"{document.label} {pseudonym}"
        references.append(
            _Reference(document.model_copy(update={"pseudonym": pseudonym, "label": label}), text)
        )
    return references, missing


def _submission_text(
    database: Store, document: ReferenceDocument, mode: SimilarityMode, store: ArtifactStore
) -> str | None:
    with database.unit_of_work() as uow:
        assert document.submission_id is not None
        submission = uow.submissions.get(document.submission_id)
    if submission is None:
        return None
    entries = _entries([submission], mode)
    _kept, contents, _skipped = _readable(entries, store)
    return contents[0].decode("utf-8", errors="replace") if contents else None


def run_references(
    database: Store,
    task_id: TaskId,
    *,
    artifact_store: ArtifactStore,
    root: Path,
    mode: SimilarityMode,
    now: datetime | None = None,
    sandbox_factory: SandboxFactory = build_tool_sandbox,
    content: ContentSettings | None = None,
    force: bool = False,
) -> SimilarityRun | None:
    """1 課題の提出を、指定した相手と 1 つの比べ方で比べる。相手が無ければ `None`。"""
    if mode not in REFERENCE_MODES or (mode is SimilarityMode.CONTENT and content is None):
        return None
    at = now or datetime.now(UTC)
    task, submissions = load_submissions(database, task_id, force=force)
    if not task.similarity_references:
        return None
    entries = _entries(submissions, mode)
    if not entries:
        return None
    references, missing = resolve(database, task, mode, artifact_store=artifact_store, root=root)
    if not references:
        if missing:
            logger.info("比べる相手が見つかりません: %s %s", task.id, missing)
        return None
    dolos = os.environ.get(ENV_SIMILARITY_IMAGE, "").strip() or DOLOS_IMAGE
    tool = content.tool if content is not None and mode is SimilarityMode.CONTENT else dolos
    params = (
        content.params if content is not None and mode is SimilarityMode.CONTENT else TEXT_PARAMS
    )
    signature = tuple(f"{r.document.pseudonym}:{r.document.content_hash}" for r in references)
    fingerprint = input_hash(entries, image=tool, params=(*params, "references", *signature))
    previous = latest_run(root, task.course_id, task.id, mode, against=True)
    if (
        not force
        and previous is not None
        and previous.input_hash == fingerprint
        and not (previous.not_measured and previous.not_measured.retryable)
    ):
        return None

    run_id = _run_id(at, mode, against=True)
    base = _base(task, fingerprint, mode=mode, tool=tool, at=at) | {
        "run_id": run_id,
        "params": params,
        "references": tuple(r.document for r in references),
        "detail": f"見つからなかった相手: {', '.join(missing)}" if missing else "",
    }

    def refuse(reason: NotMeasuredReason, detail: str, **extra: object) -> SimilarityRun:
        run = SimilarityRun.model_validate(
            base | {"not_measured": reason, "detail": detail} | extra
        )
        _write_run(task_dir(root, task.course_id, task.id) / run.run_id, run)
        _keep_only(root, run)
        return run

    kept, contents, skipped = _readable(entries, artifact_store)
    # 同じ課題どうしと違い、**提出 1 件でも相手とは比べられる**（2 件は要らない）。
    if len(kept) < 1:
        return refuse(NotMeasuredReason.TOO_FEW, f"本文を読めた提出がありません（{skipped} 件）")
    if len(kept) + len(references) > MAX_SUBMISSIONS:
        return refuse(
            NotMeasuredReason.TOO_MANY,
            f"提出 {len(kept)} 件と相手 {len(references)} 件で、1 回の上限"
            f"（{MAX_SUBMISSIONS} 件）を超えます",
        )
    texts = [c.decode("utf-8", errors="replace") for c in contents]
    started = time.monotonic()
    if mode is SimilarityMode.TEXT:
        rows, extra = _by_dolos(kept, texts, references, tool, sandbox_factory, at)
    else:
        assert content is not None
        rows, extra = _by_content(kept, texts, references, content, root)
    if isinstance(rows, NotMeasuredReason):
        return refuse(rows, str(extra.pop("detail", "")), **extra)
    duration = int((time.monotonic() - started) * 1000)

    target = task_dir(root, task.course_id, task.id) / run_id
    data = target / DATA_DIR
    data.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: (-r[2], -r[3]))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id", "leftFilePath", "rightFilePath", "similarity", "longestFragment"])
    for index, (left, right, similarity, longest) in enumerate(rows):
        writer.writerow([index, f"{left}.txt", f"{right}.txt", f"{similarity:.4f}", longest])
    (data / "pairs.csv").write_text(out.getvalue(), encoding="utf-8")
    run = SimilarityRun.model_validate(
        base
        | extra
        | {
            "language": CHAR_LANGUAGE if mode is SimilarityMode.TEXT else None,
            "submissions": tuple(
                RunSubmission(
                    submission_id=e.submission.id,
                    pseudonym=e.pseudonym,
                    filename=e.filename,
                    content_hash=e.content_hash,
                )
                for e in kept
            ),
            "baseline": round(statistics.median(r[2] for r in rows), 4) if rows else None,
            "duration_ms": duration,
        }
    )
    _write_run(target, run)
    _keep_only(root, run)
    logger.info(
        "類似の検査（相手と）: %s %s 提出 %d 件 × 相手 %d 件 %.1f 秒",
        task.id,
        mode.value,
        len(kept),
        len(references),
        duration / 1000,
    )
    return run


Row = tuple[str, str, float, int]


def _by_dolos(
    entries: Sequence[_Entry],
    texts: Sequence[str],
    references: Sequence[_Reference],
    tool: str,
    factory: SandboxFactory,
    at: datetime,
) -> tuple[list[Row] | NotMeasuredReason, dict[str, object]]:
    """Dolos で提出と相手を 1 回にまとめて比べ、**提出 × 相手の組だけ**を残す。"""
    try:
        sandbox = factory(tool)
    except SandboxError as exc:
        return NotMeasuredReason.SANDBOX_UNAVAILABLE, {"detail": str(exc)}
    names = [e.pseudonym for e in entries] + [r.document.pseudonym for r in references]
    bodies = list(texts) + [r.text for r in references]
    info = io.StringIO()
    writer = csv.writer(info, lineterminator="\n")
    writer.writerow(["filename", "id", "full_name", "labels", "created_at"])
    with sandbox.workspace() as workspace:
        for index, (name, body) in enumerate(zip(names, bodies, strict=True), start=1):
            workspace.write(f"ds/{name}.txt", body.encode("utf-8"))
            # 時刻は個人に結びつかない値にそろえる（相手の提出時刻を出さない）。
            stamp = at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
            writer.writerow([f"{name}.txt", str(index), name, "", stamp])
        workspace.write("ds/info.csv", info.getvalue())
        result = workspace.run(
            ExecRequest(
                argv=(
                    "dolos",
                    "run",
                    "-l",
                    CHAR_LANGUAGE,
                    "-f",
                    "csv",
                    "-o",
                    "out",
                    *TEXT_PARAMS,
                    "ds/info.csv",
                ),
                limits=LIMITS,
            )
        )
        pairs = workspace.path / "out" / "pairs.csv"
        if not result.ok or not pairs.is_file() or pairs.is_symlink():
            return NotMeasuredReason.TOOL_FAILED, {
                "detail": f"exit={result.exit_code} stderr={result.stderr.strip()[-300:]}",
                "sandbox": sandbox.name,
                "isolation": sandbox.isolation.value,
            }
        raw = pairs.read_text(encoding="utf-8")
    learners = {e.pseudonym for e in entries}
    rows: list[Row] = []
    for row in csv.DictReader(io.StringIO(raw)):
        left = Path(row.get("leftFilePath", "")).stem
        right = Path(row.get("rightFilePath", "")).stem
        if (left in learners) == (right in learners):
            continue  # 提出どうし・相手どうしは捨てる
        if right in learners:
            left, right = right, left
        try:
            rows.append((left, right, float(row["similarity"]), int(row["longestFragment"])))
        except (KeyError, ValueError):
            continue
    return rows, {"sandbox": sandbox.name, "isolation": sandbox.isolation.value}


def _by_content(
    entries: Sequence[_Entry],
    texts: Sequence[str],
    references: Sequence[_Reference],
    settings: ContentSettings,
    root: Path,
) -> tuple[list[Row] | NotMeasuredReason, dict[str, object]]:
    from aijudge_llm_gateway import LlmError

    try:
        mine = [_embed(settings, paragraphs(text)) for text in texts]
        theirs = [_reference_vectors(settings, r, root) for r in references]
    except LlmError as exc:
        return NotMeasuredReason.EMBEDDING_UNAVAILABLE, {"detail": str(exc)[:300]}
    rows: list[Row] = []
    for entry, left in zip(entries, mine, strict=True):
        for reference, right in zip(references, theirs, strict=True):
            if not len(left) or not len(right):
                continue
            score, close = _compare(left, right)
            rows.append((entry.pseudonym, reference.document.pseudonym, score, len(close)))
    return rows, {"tool_version": settings.model}


def _reference_vectors(settings: ContentSettings, reference: _Reference, root: Path) -> np.ndarray:
    """相手の段落の埋め込み。**束の文書はモデルごとに保存して使い回す。**"""
    document = reference.document
    if document.archive is None:
        return _embed(settings, paragraphs(reference.text))
    model = hashlib.sha256(settings.model.encode()).hexdigest()[:16]
    cache = (
        archive_root(root)
        / document.archive
        / EMBEDDING_CACHE
        / model
        / f"{document.document}-{document.content_hash[:16]}.npy"
    )
    if cache.is_file():
        cached: np.ndarray = np.load(cache)
        return cached
    vectors = _embed(settings, paragraphs(reference.text))
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache, vectors)
    return vectors


def clear_reference_runs(root: Path, task: Task) -> None:
    """相手の指定が無くなった課題の、相手と比べた回を消す。"""
    directory = task_dir(root, task.course_id, task.id)
    if not directory.is_dir():
        return
    for mode in REFERENCE_MODES:
        run = latest_run(root, task.course_id, task.id, mode, against=True)
        if run is not None:
            shutil.rmtree(directory / run.run_id, ignore_errors=True)


__all__ = ["REFERENCE_MODES", "clear_reference_runs", "resolve", "run_references"]
