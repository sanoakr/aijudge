"""教員レビューコンソール。

設計の要点は 1 つだけ。**AI の判定を見せる前に、教員の段階を確定させる。**

    待ち行列 → [blind 採点] → [AI の判定を開示] → [最終確定]
                     ↓                                  ↓
              ゴールデンセット                     成績（HumanReview）

順序を逆にすると、教員の採点は AI に引きずられる（アンカリング）。
そうして集めたデータで κ を測ると、実力より高い一致度が出る。
測定用のデータを別に集めるのではなく、**日常のレビュー作業がそのまま
正解データになる**ようにしてあるので、順序を守ることに追加コストは無い。

blind 画面のレスポンスには AI の判定を一切含めない。CSS で隠すのでは
不十分（ページのソースを見れば分かる）。これはテストで固定してある。

.. warning::

   認証は無い。単一の教員が localhost で使う前提。
   学内に公開する前に S1（Identity）に載せること。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    GradingRun,
    RubricCriterion,
    Submission,
    SubmissionState,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_grading import EvaluatorRegistry, GradingPipeline, load_profile

from .store import FinalDecision, QueueEntry, ReviewStore

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ENV_GOLDEN_DIR = "AIJUDGE_GOLDEN_DIR"
ENV_MARKER = "AIJUDGE_MARKER"
DEFAULT_GOLDEN_DIR = Path.home() / ".aijudge" / "golden"

_KINDS: dict[str, ArtifactKind] = {
    ".c": ArtifactKind.CODE,
    ".py": ArtifactKind.CODE,
    ".java": ArtifactKind.CODE,
    ".tex": ArtifactKind.LATEX,
    ".md": ArtifactKind.MARKDOWN,
}

IMPORTER = UserId("usr_" + "0" * 32)


def numbered_lines(source: str) -> list[tuple[int, str]]:
    return list(enumerate(source.replace("\r\n", "\n").split("\n"), 1))


def _submission_for(entry: QueueEntry) -> tuple[Submission, bytes]:
    payload = entry.source_path.read_bytes()
    submission_id = SubmissionId(new_id("sub"))
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=_KINDS.get(entry.source_path.suffix.lower(), ArtifactKind.MARKDOWN),
        filename=entry.submission,
        storage_key=f"file://{entry.source_path}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=datetime.now(UTC),
    )
    return (
        Submission(
            id=submission_id,
            task_version_id=TaskVersionId(new_id("tsv")),
            learner_id=UserId(new_id("usr")),
            state=SubmissionState.SUBMITTED,
            artifacts=(artifact,),
            created_at=datetime.now(UTC),
            submitted_at=datetime.now(UTC),
        ),
        payload,
    )


class Console:
    """コンソールの状態。課題定義と採点結果を必要になった時点で用意する。"""

    def __init__(
        self,
        store: ReviewStore,
        profiles_dir: Path,
        *,
        registry: EvaluatorRegistry | None = None,
        marker: str = "instructor",
        readability_weight: float = 0.3,
    ) -> None:
        self.store = store
        self.profiles_dir = profiles_dir
        self.registry = registry or EvaluatorRegistry().load_installed()
        self.marker = marker
        self.readability_weight = readability_weight
        self._tasks: dict[str, TaskVersion] = {}

    def task_for(self, entry: QueueEntry) -> TaskVersion:
        key = f"{entry.subject_profile}/{entry.task_name}"
        if key not in self._tasks:
            self._tasks[key] = sharif_judge.import_problem(
                entry.task_dir,
                subject_profile=entry.subject_profile,
                authored_by=IMPORTER,
                readability_weight=self.readability_weight,
            )
        return self._tasks[key]

    def grade(self, entry: QueueEntry) -> GradingRun:
        """未採点なら採点する。採点済みならそれを返す（毎回引き直さない）。"""
        existing = self.store.load_run(entry)
        if existing is not None:
            return existing

        task = self.task_for(entry)
        profile = load_profile(self.profiles_dir / f"{entry.subject_profile}.yaml", self.registry)
        submission, payload = _submission_for(entry)
        run = GradingPipeline(self.registry, profile).run(task, submission, lambda _: payload)
        self.store.save_run(entry, run)
        return run


def create_app(console: Console, *, min_sample_size: int = 30) -> FastAPI:
    app = FastAPI(title="aiJudge review console")

    def _entry_or_404(entry_id: str) -> QueueEntry:
        entry = console.store.find(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown submission: {entry_id}")
        return entry

    # -- 待ち行列 ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        entries = console.store.queue()
        reviewed = [entry for entry in entries if entry.reviewed]
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "entries": entries,
                "reviewed_count": len(reviewed),
                "pending_count": len(entries) - len(reviewed),
                "min_sample_size": min_sample_size,
            },
        )

    # -- blind 採点 --------------------------------------------------------

    @app.get("/review/{entry_id:path}/blind", response_class=HTMLResponse)
    def blind(request: Request, entry_id: str) -> HTMLResponse:
        """AI の判定を**一切含めない**画面。

        採点はこの時点で走らせない。走らせて隠すのではなく、
        そもそもレスポンスに載せない。
        """
        entry = _entry_or_404(entry_id)
        task = console.task_for(entry)
        return TEMPLATES.TemplateResponse(
            request,
            "blind.html",
            {
                "entry": entry,
                "task": task,
                "lines": numbered_lines(entry.source_path.read_text(encoding="utf-8")),
                "criteria": task.criteria,
            },
        )

    @app.post("/review/{entry_id:path}/blind")
    def submit_blind(
        entry_id: str,
        levels: Annotated[list[str], Form()],
        notes: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """blind 採点を保存し、そこで初めて AI に採点させる。"""
        entry = _entry_or_404(entry_id)
        task = console.task_for(entry)
        parsed = _parse_levels(task.criteria, levels)

        console.store.save_blind_mark(
            entry, levels=parsed, marker=console.marker, notes=notes.strip() or None
        )
        console.grade(entry)
        return RedirectResponse(f"/review/{entry_id}/reveal", status_code=303)

    # -- 開示と確定 --------------------------------------------------------

    @app.get("/review/{entry_id:path}/reveal", response_class=HTMLResponse)
    def reveal(request: Request, entry_id: str) -> HTMLResponse:
        entry = _entry_or_404(entry_id)
        if not entry.reviewed:
            # blind 採点を飛ばして AI の判定を見ることはできない。
            return RedirectResponse(f"/review/{entry_id}/blind", status_code=303)

        task = console.task_for(entry)
        run = console.grade(entry)
        blind_levels = _load_blind_levels(entry)
        source = entry.source_path.read_text(encoding="utf-8")

        return TEMPLATES.TemplateResponse(
            request,
            "reveal.html",
            {
                "entry": entry,
                "task": task,
                "run": run,
                "lines": numbered_lines(source),
                "rows": _comparison_rows(task, run, blind_levels),
                "highlights": _highlighted_lines(run),
                "decision": console.store.load_decision(entry),
            },
        )

    @app.post("/review/{entry_id:path}/finalize")
    def finalize(
        entry_id: str,
        levels: Annotated[list[str], Form()],
        comment: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        entry = _entry_or_404(entry_id)
        task = console.task_for(entry)
        run = console.grade(entry)
        final = _parse_levels(task.criteria, levels)
        blind_levels = _load_blind_levels(entry)

        console.store.save_decision(
            entry,
            FinalDecision(
                grading_run_id=run.id,
                grader=console.marker,
                # ゴールデンセットに残るのは blind の側。ここで上書きしない。
                blind_levels=blind_levels,
                final_levels=final,
                changed_after_seeing_ai=final != blind_levels,
                comment=comment.strip() or None,
                decided_at=datetime.now(UTC),
            ),
        )
        return RedirectResponse("/", status_code=303)

    return app


# -- ヘルパ ----------------------------------------------------------------


def _parse_levels(criteria: tuple[RubricCriterion, ...], raw: list[str]) -> dict[str, int]:
    """フォームの `code=level` を辞書にする。観点の取りこぼしは拒否する。"""
    parsed: dict[str, int] = {}
    for item in raw:
        code, _, value = item.partition("=")
        try:
            parsed[code] = int(value)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"malformed level: {item!r}") from None

    expected = {criterion.code for criterion in criteria}
    if set(parsed) != expected:
        missing = sorted(expected - set(parsed))
        raise HTTPException(status_code=400, detail=f"missing marks for: {missing}")

    for criterion in criteria:
        allowed = {level.level for level in criterion.levels}
        if parsed[criterion.code] not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{criterion.code}: level {parsed[criterion.code]} is not in {sorted(allowed)}"
                ),
            )
    return parsed


def _load_blind_levels(entry: QueueEntry) -> dict[str, int]:
    import yaml

    data = yaml.safe_load(entry.mark_path.read_text(encoding="utf-8"))
    return {str(code): int(level) for code, level in data["marks"].items()}


def _comparison_rows(
    task: TaskVersion, run: GradingRun, blind_levels: dict[str, int]
) -> list[dict[str, object]]:
    """観点ごとに「教員 vs AI」を並べる。"""
    by_id = {score.criterion_id: score for score in run.criterion_scores}
    rows: list[dict[str, object]] = []
    for criterion in task.criteria:
        score = by_id.get(criterion.id)
        human = blind_levels.get(criterion.code)
        rows.append(
            {
                "criterion": criterion,
                "human_level": human,
                "ai_level": None if score is None else score.level,
                "agrees": score is not None and human == score.level,
                "score": score,
                "unscored": criterion.id in run.unscored_criteria,
            }
        )
    return rows


def _highlighted_lines(run: GradingRun) -> set[int]:
    """AI が根拠として挙げた行。コードの上で目立たせる。"""
    lines: set[int] = set()
    for score in run.criterion_scores:
        for evidence in score.evidence:
            span = evidence.span
            if span.kind == "line":
                lines.update(range(span.start_line, span.end_line + 1))
    return lines


def build_app() -> FastAPI:
    """`uvicorn aijudge_reviewconsole.app:build_app --factory` 用。"""
    root = Path(os.environ.get(ENV_GOLDEN_DIR, DEFAULT_GOLDEN_DIR)).expanduser()
    profiles = Path(__file__).resolve().parents[4] / "subjects"
    console = Console(
        ReviewStore(root),
        profiles,
        marker=os.environ.get(ENV_MARKER, "instructor"),
    )
    return create_app(console)
