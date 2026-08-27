"""教員レビューコンソール。

**この画面は採点しない。** 採点は提出時にワーカーが走らせ（`worker.py`）、
ここは届いた結果を読んで教員が確定させる場所である。以前はレビューが採点を
起動していたため、測定用データの入力が採点の前提条件になっていた。これは
手段と目的が逆で、測定を必須機能にしない方針に反する（ADR 0007）。

通常の経路（大多数の提出）:

    待ち行列 → [AI の判定を見て確定] → 成績

blind 抽出に当たった提出だけ、教員の段階を先に取る:

    待ち行列 → [blind 採点] → [AI の判定を開示] → [確定]
                     ↓                              ↓
              測定用の正解データ                  成績

順序を逆にすると教員の採点は AI に引きずられる（アンカリング）。そうして
集めたデータで κ を測れば、実力より高い一致度が出る。だが全件に課すと
レビュー 1 件ごとに 2 段階の入力を強制することになるので、抽出に留める。
抽出率は科目プロファイルの `measurement.blind_sample_rate` で宣言し、
対象の選定はハッシュで決める（教員に選ばせると選択バイアスが入る）。

blind 画面のレスポンスには AI の判定を一切含めない。CSS で隠すのでは
不十分（ページのソースを見れば分かる）。これはテストで固定してある。

.. warning::

   認証は無い。単一の教員が localhost で使う前提。
   学内に公開する前に S1（Identity）に載せること。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aijudge_core import GradingRun, RubricCriterion, TaskVersion
from aijudge_grading import EvaluatorRegistry, load_profile

from .projection import project
from .store import FinalDecision, GoldenMark, QueueEntry, ReviewStore, is_blind_sample
from .tasks import TaskLoader

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ENV_GOLDEN_DIR = "AIJUDGE_GOLDEN_DIR"
ENV_MARKER = "AIJUDGE_MARKER"
DEFAULT_GOLDEN_DIR = Path.home() / ".aijudge" / "golden"


def numbered_lines(source: str) -> list[tuple[int, str]]:
    return list(enumerate(source.replace("\r\n", "\n").split("\n"), 1))


class Console:
    """コンソールの状態。

    **採点機能を持たない。** 採点は `worker.Grader` の仕事で、ここからは
    呼べない。呼べるようにしておくと、いつかどこかの経路でレビューが採点を
    起動し、測定用データの入力が採点の前提条件に戻る（ADR 0007）。
    """

    def __init__(
        self,
        store: ReviewStore,
        profiles_dir: Path,
        *,
        registry: EvaluatorRegistry | None = None,
        marker: str = "instructor",
        tasks: TaskLoader | None = None,
    ) -> None:
        self.store = store
        self.profiles_dir = profiles_dir
        self.registry = registry
        self.marker = marker
        self.tasks = tasks or TaskLoader()
        self._rates: dict[str, float] = {}

    def task_for(self, entry: QueueEntry) -> TaskVersion:
        return self.tasks.task_for(entry)

    def blind_sample_rate(self, subject_profile: str) -> float:
        """科目プロファイルが宣言した blind 抽出率。

        プロファイルが読めない場合は 0 とする。**測定のために採点や
        レビューを止めない。** 抽出されないだけで運用は続く。
        """
        if subject_profile not in self._rates:
            path = self.profiles_dir / f"{subject_profile}.yaml"
            try:
                # registry は渡さない。ここで評価器の実在まで検証する必要はなく、
                # 検証は採点側（worker）が行う。
                rate = load_profile(path).measurement.blind_sample_rate
            except Exception:
                rate = 0.0
            self._rates[subject_profile] = rate
        return self._rates[subject_profile]

    def entries(self, subject_profile: str | None = None) -> tuple[QueueEntry, ...]:
        """待ち行列に blind 抽出の判定を付けて返す。"""
        return tuple(
            entry.model_copy(
                update={
                    "blind_required": is_blind_sample(
                        entry.id, self.blind_sample_rate(entry.subject_profile)
                    )
                }
            )
            for entry in self.store.queue(subject_profile)
        )

    def entry(self, entry_id: str) -> QueueEntry | None:
        return next((entry for entry in self.entries() if entry.id == entry_id), None)

    def refresh_observations(self, entry: QueueEntry, run: GradingRun) -> None:
        """確定後に観測を書き直す。**失敗してもレビューは成立させる。**

        観測は投影であって記録の正本ではない（正本は runs/ と reviews/）。
        測定の都合でレビューを落とさない（ADR 0007）。
        """
        try:
            self.store.save_observations(
                entry,
                project(
                    entry,
                    self.task_for(entry),
                    run,
                    mark=self.store.load_blind_mark(entry),
                    decision=self.store.load_decision(entry),
                ),
            )
        except Exception:
            return


def create_app(console: Console, *, min_sample_size: int = 30) -> FastAPI:
    app = FastAPI(title="aiJudge review console")

    def _entry_or_404(entry_id: str) -> QueueEntry:
        entry = console.entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown submission: {entry_id}")
        return entry

    def _run_or_409(entry: QueueEntry) -> GradingRun:
        """採点結果を読む。無ければ 409。

        **ここで採点しない。** 採点はワーカーが提出時に走らせる。まだ届いて
        いない状態は正常であり（AI 評価は非同期）、レビュー側は待つだけ。
        """
        run = console.store.load_run(entry)
        if run is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{entry.id} はまだ採点されていません。"
                    "`uv run aijudge-grade` で採点してください。"
                ),
            )
        return run

    # -- 待ち行列 ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        entries = console.entries()
        marked = [entry for entry in entries if entry.marked]
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "entries": entries,
                "marked_count": len(marked),
                "pending_count": sum(1 for entry in entries if entry.pending),
                "ungraded_count": sum(1 for entry in entries if not entry.graded),
                "blind_pending_count": sum(1 for entry in entries if entry.needs_blind_mark),
                "min_sample_size": min_sample_size,
            },
        )

    # -- blind 採点（抽出対象のみ）----------------------------------------

    @app.get("/review/{entry_id:path}/blind", response_class=HTMLResponse)
    def blind(request: Request, entry_id: str) -> HTMLResponse:
        """AI の判定を**一切含めない**画面。

        採点済みかどうかに関わらず、この画面には判定を載せない。隠すのでは
        なく、レスポンスに含めない。
        """
        entry = _entry_or_404(entry_id)
        if not entry.blind_required:
            # 抽出対象でない提出に blind 採点を求めない。
            return RedirectResponse(f"/review/{entry_id}/reveal", status_code=303)

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
        """blind 採点を保存する。**採点は起動しない。**

        以前はここで採点を走らせていた。それは測定用データの入力を採点の
        前提条件にすることで、方針が逆転していた（ADR 0007）。
        """
        entry = _entry_or_404(entry_id)
        task = console.task_for(entry)
        parsed = _parse_levels(task.criteria, levels)

        console.store.save_blind_mark(
            entry, levels=parsed, marker=console.marker, notes=notes.strip() or None
        )
        # 既に採点が届いていれば、観測に教員の段階を反映しておく。
        run = console.store.load_run(entry)
        if run is not None:
            console.refresh_observations(entry.model_copy(update={"marked": True}), run)
        return RedirectResponse(f"/review/{entry_id}/reveal", status_code=303)

    # -- 開示と確定 --------------------------------------------------------

    @app.get("/review/{entry_id:path}/reveal", response_class=HTMLResponse)
    def reveal(request: Request, entry_id: str) -> HTMLResponse:
        entry = _entry_or_404(entry_id)
        if entry.needs_blind_mark:
            # 抽出対象で未採点なら、先に教員の段階を取る。
            return RedirectResponse(f"/review/{entry_id}/blind", status_code=303)

        run = _run_or_409(entry)
        task = console.task_for(entry)
        mark = console.store.load_blind_mark(entry)
        blind_levels = {} if mark is None else dict(mark.marks)
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
                "was_blind": mark is not None,
            },
        )

    @app.post("/review/{entry_id:path}/finalize")
    def finalize(
        entry_id: str,
        levels: Annotated[list[str], Form()],
        comment: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        entry = _entry_or_404(entry_id)
        run = _run_or_409(entry)
        task = console.task_for(entry)
        final = _parse_levels(task.criteria, levels)
        mark = console.store.load_blind_mark(entry)

        # blind 採点が無い提出では、AI の判定と比べて変えたかどうかを見る。
        # blind があるならそちらと比べる（AI を見る前の段階が基準）。
        reference = _reference_levels(task, run, mark)

        console.store.save_decision(
            entry,
            FinalDecision(
                grading_run_id=run.id,
                grader=console.marker,
                # 測定に使うのは blind の側。ここで上書きしない。
                blind_levels={} if mark is None else dict(mark.marks),
                final_levels=final,
                changed_after_seeing_ai=final != reference,
                comment=comment.strip() or None,
                decided_at=datetime.now(UTC),
            ),
        )
        console.refresh_observations(entry.model_copy(update={"decided": True}), run)
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


def _reference_levels(
    task: TaskVersion, run: GradingRun, mark: GoldenMark | None
) -> dict[str, int]:
    """「変えた」の基準になる段階。

    blind 採点があるならそれ（AI を見る前の教員の判断）。無ければ AI の判定。
    見逃し率は「AI をそのまま通したのに、実は直すべきだった」を測る指標なので、
    抽出対象外の提出では AI の判定が基準になる。
    """
    if mark is not None:
        return dict(mark.marks)
    by_id = {score.criterion_id: score for score in run.criterion_scores}
    return {
        criterion.code: by_id[criterion.id].level
        for criterion in task.criteria
        if criterion.id in by_id
    }


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
                # blind 採点が無い提出では突き合わせるものが無い。
                # None は「不一致」ではなく「比べていない」を表す。
                "agrees": None if (human is None or score is None) else human == score.level,
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
