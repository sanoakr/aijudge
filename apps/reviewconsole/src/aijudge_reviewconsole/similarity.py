"""提出どうしの類似を教員が見る画面（#203・ADR 0029）。

**見られるのはそのコースの担当教員だけ**（TA には開けない）。報告は全員のコードの写しで、
仮の名前（`S-001`）と学習者の対応も出るので、**見たことを監査ログに残す**
（`AuditAction.SIMILARITY_VIEWED`）。**判定はしない** ── 同じ課題を解けば似る。

画面は 2 つある。

- 入口（`/courses/{id}/similarity`）: 問題セット・課題ごとの回、上位の組、仮の名前と
  学習者の対応。`run.json` と `pairs.csv` だけで作る（DB に表は無い）
- Dolos の画面（`/courses/{id}/similarity/{課題}/{回}/`）: Dolos の web（静的ファイル）を
  そのまま配り、その回の CSV を `data/` の下に置く。Dolos の画面はデータの場所を
  **ページの URL からの相対**で決める（`location.pathname + "data"`）ので、この形で動く

Dolos の画面には **CSP で外への通信を塞ぐ**。配布物は Google Fonts を読みに行く
（止めても表示は崩れない）。その他の外部 URL は押したときだけ開くリンクである。
配布物は `aijudge-admin similarity install-viewer` が、検査と同じイメージから取り出す。
"""

from __future__ import annotations

import csv
import mimetypes
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from aijudge_audit import AuditAction
from aijudge_core.ids import CourseId, TaskId
from aijudge_course_admin.code_similarity import (
    DATA_DIR,
    REPORT_FILES,
    SimilarityRun,
    list_runs,
    read_run,
    run_dir,
    similarity_root,
    task_dir,
)

from .audit_context import recorder_for
from .urls import root_prefix

#: Dolos の画面の配布物（`index.html` と `assets/`）の置き場所。
ENV_DOLOS_WEB_DIR = "AIJUDGE_DOLOS_WEB_DIR"
#: 入口に出す上位の組の数（課題ごと）。全部は Dolos の画面で見る。
TOP_PAIRS = 5

#: Dolos の画面に付ける CSP。**外への通信を全部止める**（配布物は Google Fonts を
#: 読みに行く）。Monaco の差分とワーカーのために `blob:` を許す。手元の Chromium で、
#: この CSP の下で 6 画面（概要・組・提出・グラフ・クラスタ・比較）が表示されることを
#: 確かめた（2026-09-29）。
VIEWER_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data: blob:; worker-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_VIEWER_HEADERS = {
    "Content-Security-Policy": VIEWER_CSP,
    # URL に課題と回が入る。リンクを押しても外へ渡さない。
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}
_ASSET_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}

REASON_LABELS = {
    "too_few": "提出が 2 件未満",
    "too_many": "提出が多すぎる",
    "unsupported_language": "C・Python 以外のファイルがある",
    "mixed_languages": "言語が混ざっている",
    "sandbox_unavailable": "実行環境が使えなかった（次の周回で回し直します）",
    "tool_failed": "検査が失敗した（次の周回で回し直します）",
}


def viewer_root() -> Path | None:
    raw = os.environ.get(ENV_DOLOS_WEB_DIR, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if (path / "index.html").is_file() else None


def top_pairs(run: SimilarityRun, root: Path, limit: int = TOP_PAIRS) -> list[dict[str, Any]]:
    """`pairs.csv` の上位。**名前は仮のまま返す**（学習者への対応は呼ぶ側が付ける）。"""
    path = run_dir(root, run) / DATA_DIR / "pairs.csv"
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return []
    pairs: list[dict[str, Any]] = []
    for row in rows:
        try:
            similarity = float(row["similarity"])
            longest = int(row["longestFragment"])
        except (KeyError, ValueError):
            continue
        pairs.append(
            {
                "left": Path(row.get("leftFilePath", "")).stem,
                "right": Path(row.get("rightFilePath", "")).stem,
                "similarity": similarity,
                "longest": longest,
            }
        )
    pairs.sort(key=lambda p: (-p["similarity"], -p["longest"]))
    return pairs[:limit]


def register(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _instructor(request: Request, course_id: str):
        # 管理画面と同じ「教員だけ」の判定（TA には開けない）。作業の記録と同じ。
        from .app import require_principal
        from .manage.common import _require_instructor

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return me, course

    def _audit(request: Request, me, course_id: str, target: str, what: str, detail: dict):
        console = request.app.state.aijudge
        with console.database.unit_of_work() as uow:
            recorder_for(uow, request, me, role="instructor").record(
                AuditAction.SIMILARITY_VIEWED,
                target_type="task" if target != course_id else "course",
                target_id=target,
                summary=f"提出どうしの類似を見た（{what}）",
                detail={"course_id": course_id, **detail},
            )
            uow.commit()

    def _run(course_id: CourseId, task_id: str, run_id: str) -> tuple[Path, SimilarityRun]:
        root = similarity_root()
        if root is None:
            raise HTTPException(status_code=404, detail="報告が見つかりません")
        # 経路の部品をそのままパスにしない。課題の下の回の名前と突き合わせる。
        directory = task_dir(root, course_id, TaskId(task_id))
        candidates = {p.name: p for p in directory.iterdir()} if directory.is_dir() else {}
        target = candidates.get(run_id)
        if target is None or not target.is_dir():
            raise HTTPException(status_code=404, detail="報告が見つかりません")
        run = read_run(target)
        if run is None or not run.measured or str(run.course_id) != str(course_id):
            raise HTTPException(status_code=404, detail="報告が見つかりません")
        return root, run

    @router.get("/courses/{course_id}/similarity", response_class=HTMLResponse)
    def course_similarity(request: Request, course_id: str) -> HTMLResponse:
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        root = similarity_root()
        runs = list_runs(root, course.id) if root is not None else []
        units: dict[str, list[dict[str, Any]]] = {}
        with console.database.unit_of_work() as uow:
            # 課題の並びはコースの一覧と同じ（`list_for_course` の順）。
            order = {t.id: i for i, t in enumerate(uow.tasks.list_for_course(course.id))}
            for run in sorted(runs, key=lambda r: order.get(r.task_id, len(order))):
                people: dict[str, tuple[str, str]] = {}
                for item in run.submissions:
                    submission = uow.submissions.get(item.submission_id)
                    user = uow.identity.get_user(submission.learner_id) if submission else None
                    people[item.pseudonym] = (
                        user.login if user else "?",
                        user.display_name if user else "",
                    )
                pairs = top_pairs(run, root) if run.measured and root is not None else []
                units.setdefault(run.unit or "未分類", []).append(
                    {
                        "run": run,
                        "people": people,
                        "pairs": pairs,
                        "reason": REASON_LABELS.get(
                            run.not_measured.value if run.not_measured else "", ""
                        ),
                        "href": f"/courses/{course.id}/similarity/{run.task_id}/{run.run_id}/",
                    }
                )
        _audit(request, me, str(course.id), str(course.id), "入口", {"runs": len(runs)})
        return templates.TemplateResponse(
            request,
            "similarity_course.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "提出の類似", "href": f"/courses/{course.id}/similarity"},
                "units": units,
                "configured": root is not None,
                "viewer": viewer_root() is not None,
            },
        )

    @router.get("/courses/{course_id}/similarity/{task_id}/{run_id}")
    def viewer_without_slash(request: Request, course_id: str, task_id: str, run_id: str):
        # Dolos の画面は URL の末尾から `data` を引くので、`/` で終わらせる。
        _instructor(request, course_id)
        return RedirectResponse(
            f"{root_prefix()}/courses/{course_id}/similarity/{task_id}/{run_id}/",
            status_code=307,
        )

    @router.get("/courses/{course_id}/similarity/{task_id}/{run_id}/")
    def viewer(request: Request, course_id: str, task_id: str, run_id: str) -> Response:
        me, course = _instructor(request, course_id)
        _root, run = _run(course.id, task_id, run_id)
        web = viewer_root()
        if web is None:
            raise HTTPException(
                status_code=503,
                detail="Dolos の画面が用意されていません（similarity install-viewer）",
            )
        _audit(
            request,
            me,
            str(course.id),
            str(run.task_id),
            f"{run.task_title}の報告",
            {"run_id": run.run_id},
        )
        return Response(
            content=(web / "index.html").read_bytes(),
            media_type="text/html",
            headers=_VIEWER_HEADERS,
        )

    @router.get("/courses/{course_id}/similarity/{task_id}/{run_id}/{path:path}")
    def viewer_file(
        request: Request, course_id: str, task_id: str, run_id: str, path: str
    ) -> Response:
        """画面の部品と、その回の CSV。

        - **CSV は開くたびに担当教員かを確かめる**（全員のコードが入っている）
        - **部品（`assets/`）は確かめない。** 中身は公開されている Dolos の配布物で、学習者の
          データは無い。1 回開くと部品が約 150 本並行して来る ── 1 本ずつセッションと受講を
          DB に問い合わせていたら、手元の確認で一部が 401・404 で欠けた（2026-09-29）。
          配布物の外へは出さない

        監査は画面を開いたとき（`index.html`）に 1 行だけ残す ── 部品ごとに残すと、
        1 回の閲覧で数十行になる。
        """
        if path.startswith(f"{DATA_DIR}/"):
            _me, course = _instructor(request, course_id)
            root, run = _run(course.id, task_id, run_id)
            name = path[len(DATA_DIR) + 1 :]
            if name not in REPORT_FILES:
                raise HTTPException(status_code=404, detail="見つかりません")
            target = run_dir(root, run) / DATA_DIR / name
            media_type = "text/csv"
        else:
            web = viewer_root()
            if web is None:
                raise HTTPException(status_code=404, detail="見つかりません")
            target = (web / path).resolve()
            # 配布物の外へは出さない（`..` やリンクで抜ける道を塞ぐ）。
            if not target.is_relative_to(web.resolve()) or not target.is_file():
                raise HTTPException(status_code=404, detail="見つかりません")
            media_type = _ASSET_TYPES.get(target.suffix) or (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            # 部品の名前にはハッシュが入っている（中身が変われば名前が変わる）。
            return Response(
                content=target.read_bytes(),
                media_type=media_type,
                headers={**_VIEWER_HEADERS, "Cache-Control": "private, max-age=86400"},
            )
        if not target.is_file():
            raise HTTPException(status_code=404, detail="見つかりません")
        return Response(content=target.read_bytes(), media_type=media_type, headers=_VIEWER_HEADERS)

    return router
