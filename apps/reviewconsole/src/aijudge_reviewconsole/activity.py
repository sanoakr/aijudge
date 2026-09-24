"""IDE の行動記録を教員が見る画面（ADR 0023 §5・設計書 §7）。

**見られるのはそのコースの教員だけ**（TA には開けない）。学習者本人のデータ
なので、**見たことを監査ログに残す**（`AuditAction.ACTIVITY_VIEWED`）。

画面が出すのは**事実だけ**である。数字（外からの貼り付けの字数、画面を離れて
いた時間）と、記録の欠け・組み立て直しの食い違いを並べ、経過を再生できるように
する。**判定はしない**（設計書 §7）── 誤検知の不利益が大きすぎる。成績にも
つながない（`grading-does-not-know-ide`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_audit import AuditAction
from aijudge_core.ids import CourseId, SubmissionId, TaskVersionId, UserId
from aijudge_ide import (
    ActivityFiles,
    ActivitySummary,
    EventBatch,
    Flag,
    IdeSession,
    IdeSessionId,
    IntegrityReport,
    check_session,
    flag_events,
    submission_mismatches,
    summarize,
)

from .audit_context import recorder_for

# 行動記録の本体の置き場所（web と同じ変数・`aijudge_studentweb.cli`）。
ENV_ACTIVITY_DIR = "AIJUDGE_ACTIVITY_DIR"


def activity_root() -> Path | None:
    """本体の置き場所。**無ければ None**（画面は「設定されていない」と言う）。"""
    configured = os.environ.get(ENV_ACTIVITY_DIR)
    return Path(configured).expanduser() if configured else None


@dataclass
class SessionView:
    session: IdeSession
    batches: int
    summary: ActivitySummary
    integrity: IntegrityReport
    # ファイルが読めなかった束の数（索引はあるのに本体が無い）。
    unreadable: int
    # 確かめる価値のある場所の目印（`aijudge_ide.flags`）。**判定ではない。**
    flags: list[Flag]


def _read(
    files: ActivityFiles, session: IdeSession, batches: tuple[EventBatch, ...]
) -> tuple[list[tuple[EventBatch, list[dict[str, Any]]]], int]:
    loaded: list[tuple[EventBatch, list[dict[str, Any]]]] = []
    unreadable = 0
    for batch in batches:
        try:
            loaded.append((batch, files.read_batch(batch.path)))
        except (OSError, ValueError):
            unreadable += 1
    return loaded, unreadable


def _flags(console, events: list[dict[str, Any]]) -> list[Flag]:
    """印を付ける。提出の食い違いは、実際の提出の指紋（出どころの記録）と比べる。"""
    submitted: dict[str, str] = {}
    with console.database.unit_of_work() as uow:
        for event in events:
            if event.get("type") == "submit" and event.get("submission_id"):
                link = uow.ide_links.for_submission(SubmissionId(str(event["submission_id"])))
                if link is not None:
                    submitted[str(link.submission_id)] = link.content_hash
    flags = flag_events(events) + submission_mismatches(events, submitted)
    return sorted(flags, key=lambda flag: flag.t)


def register(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _instructor(request: Request, course_id: str):
        # 管理画面と同じ「教員だけ」の判定を使う（TA には開けない）。
        from .app import require_principal
        from .manage import _require_instructor

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return me, course

    def _audit(request: Request, me, course_id: str, learner_id: str, what: str, detail: dict):
        console = request.app.state.aijudge
        with console.database.unit_of_work() as uow:
            recorder_for(uow, request, me, role="instructor").record(
                AuditAction.ACTIVITY_VIEWED,
                target_type="learner",
                # 対象は「どのコースの誰か」の対（受講登録と同じ名指し方）。
                target_id=f"{course_id}:{learner_id}",
                summary=f"エディタの作業記録を見た（{what}）",
                detail={"course_id": course_id, **detail},
            )
            uow.commit()

    @router.get("/courses/{course_id}/activity/{learner_id}", response_class=HTMLResponse)
    def learner_activity(request: Request, course_id: str, learner_id: str) -> HTMLResponse:
        """この学習者のエディタでの作業の一覧（セッションごとの要約と欠け）。"""
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        root = activity_root()
        with console.database.unit_of_work() as uow:
            learner = uow.identity.get_user(UserId(learner_id))
            enrolled = uow.identity.find_enrollment(course.id, UserId(learner_id))
            sessions = uow.ide_activity.sessions_for(UserId(learner_id), course.id)
            indexed = {s.id: uow.ide_activity.batches(s.id) for s in sessions}
        # 受講していない人の記録は、このコースの画面からは見せない。
        if learner is None or enrolled is None:
            raise HTTPException(status_code=404, detail="学習者が見つかりません")

        views: list[SessionView] = []
        if root is not None:
            files = ActivityFiles(root)
            for session in sessions:
                loaded, unreadable = _read(files, session, indexed[session.id])
                session_events = [e for _b, events in loaded for e in events]
                views.append(
                    SessionView(
                        session=session,
                        batches=len(indexed[session.id]),
                        summary=summarize(session_events),
                        integrity=check_session(
                            loaded, lambda name, s=session: files.read_snapshot(s, name)
                        ),
                        unreadable=unreadable,
                        flags=_flags(console, session_events),
                    )
                )
        _audit(request, me, str(course.id), learner_id, "一覧", {"sessions": len(sessions)})
        return templates.TemplateResponse(
            request,
            "activity_learner.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "提出", "href": f"/courses/{course.id}/submissions"},
                "learner": learner,
                "views": views,
                "configured": root is not None,
                "session_count": len(sessions),
            },
        )

    @router.get(
        "/courses/{course_id}/activity/{learner_id}/{session_id}", response_class=HTMLResponse
    )
    def session_replay(
        request: Request, course_id: str, learner_id: str, session_id: str
    ) -> HTMLResponse:
        """1 回のセッションを時刻順に再生する。"""
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        root = activity_root()
        with console.database.unit_of_work() as uow:
            session = uow.ide_activity.get_session(IdeSessionId(session_id))
            if (
                session is None
                or str(session.learner_id) != learner_id
                or session.course_id != course.id
            ):
                raise HTTPException(status_code=404, detail="記録が見つかりません")
            batches = uow.ide_activity.batches(session.id)
            learner = uow.identity.get_user(session.learner_id)
        if root is None:
            raise HTTPException(status_code=503, detail="記録の置き場所が設定されていません")

        files = ActivityFiles(root)
        loaded, unreadable = _read(files, session, batches)
        events = [dict(e, seq=b.seq) for b, batch_events in loaded for e in batch_events]
        # 再生に要る全文: 開いたときの各タブ、読み込んだファイル、束の末尾の指紋。
        wanted: set[str] = set()
        tab_titles: list[str] = []
        for event in events:
            kind = event.get("type")
            if kind in ("hello", "tabs"):
                wanted.update(h for h in event.get("hashes") or [] if h)
            elif kind == "file_load" and event.get("hash"):
                wanted.add(str(event["hash"]))
            if kind == "hello" and not tab_titles:
                tab_titles = _titles(console, event.get("tasks") or [])
        snapshots = {
            name: text
            for name in wanted
            if (text := files.read_snapshot(session, name)) is not None
        }
        integrity = check_session(loaded, lambda name: files.read_snapshot(session, name))
        # 印は再生の目印の一覧に並べ、押すとその時点へ飛ぶ。
        flags = [
            {"t": flag.t, "label": flag.label, "detail": flag.detail}
            for flag in _flags(console, events)
        ]
        _audit(
            request,
            me,
            str(course.id),
            learner_id,
            "再生",
            {"session_id": str(session.id)},
        )
        return templates.TemplateResponse(
            request,
            "activity_session.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "提出", "href": f"/courses/{course.id}/submissions"},
                "learner": learner,
                "session": session,
                "integrity": integrity,
                "unreadable": unreadable,
                "flag_count": len(flags),
                # **`<` をすべて逃がす**（`\u003c`、JSON としてはそのまま読める）。
                # 学習者のコードに `</script>` があると埋め込みから抜け出せ、
                # `<!--` や `<script` でも HTML の読み取りが崩れる。教員の画面で、
                # 教員の権限で動くスクリプトを差し込ませない。
                "replay_json": json.dumps(
                    {
                        "events": events,
                        "snapshots": snapshots,
                        "titles": tab_titles,
                        "flags": flags,
                    },
                    ensure_ascii=False,
                ).replace("<", "\\u003c"),
            },
        )

    return router


def _titles(console, version_ids: list[str]) -> list[str]:
    """タブの課題名。消えた課題は ID のまま出す（記録は課題より長く残りうる）。"""
    titles: list[str] = []
    with console.database.unit_of_work() as uow:
        for version_id in version_ids:
            version = uow.tasks.get_version(TaskVersionId(str(version_id)))
            task = None if version is None else uow.tasks.get_task(version.task_id)
            titles.append(task.title if task is not None else str(version_id))
    return titles
