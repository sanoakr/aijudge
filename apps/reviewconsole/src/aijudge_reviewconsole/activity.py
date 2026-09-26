"""IDE の行動記録を教員が見る画面（ADR 0023 §5・設計書 §7）。

**見られるのはそのコースの教員だけ**（TA には開けない）。学習者本人のデータ
なので、**見たことを監査ログに残す**（`AuditAction.ACTIVITY_VIEWED`）。

画面が出すのは**事実だけ**である。数字（外からの貼り付けの字数、画面を離れて
いた時間）と、記録の欠け・組み立て直しの食い違いを並べ、経過を再生できるように
する。**判定はしない**（設計書 §7）── 誤検知の不利益が大きすぎる。成績にも
つながない（`grading-does-not-know-ide`）。

画面は 3 つある。

- コース全体の一覧（`/courses/{id}/activity`）: **索引だけで作る**（本体を読まない）。
  受講者全員について、エディタを開いた回数・最後に開いた時刻・送信の欠け。
  提出していない学習者にもここから辿れる
- 学習者ごと（`/courses/{id}/activity/{learner}`）: **問題ごとに分ける**。教員が
  知りたいのは「この問題をどう書いたか」で、1 回のセッションで複数の問題を行き
  来するので、セッションの合計では読めない（2026-09-24 決定）
- 再生（`/courses/{id}/activity/{learner}/{session}?tab=N`）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_audit import AuditAction
from aijudge_core import Role, attempt_ordinals
from aijudge_core.ids import CourseId, SubmissionId, TaskVersionId, UserId
from aijudge_ide import (
    ActivityFiles,
    ActivitySummary,
    EventBatch,
    IdeSession,
    IdeSessionId,
    IntegrityReport,
    check_session,
    summarize_by_tab,
)
from aijudge_ide.flags import Flag, flag_events, shared_paste_flags, submission_mismatches

from .audit_context import recorder_for
from .submissions import adopted_ids, version_max_scores

# 行動記録の本体の置き場所（web と同じ変数・`aijudge_studentweb.cli`）。
ENV_ACTIVITY_DIR = "AIJUDGE_ACTIVITY_DIR"
# 問題が分からないタブ（hello に課題の一覧が無い古い記録）をまとめる鍵。
UNKNOWN_TASK = "?"


def activity_root() -> Path | None:
    """本体の置き場所。**無ければ None**（画面は「設定されていない」と言う）。"""
    configured = os.environ.get(ENV_ACTIVITY_DIR)
    return Path(configured).expanduser() if configured else None


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


def _missing(batches: tuple[EventBatch, ...]) -> int:
    """索引だけで数える送信の欠け（seq の飛び）。本体を読まずに済む。"""
    seqs = sorted(batch.seq for batch in batches)
    if not seqs:
        return 0
    return (seqs[-1] + 1) - len(set(seqs))


def _flags(console, events: list[dict[str, Any]], session: IdeSession) -> list[Flag]:
    """印を付ける。提出の食い違いは、実際の提出の指紋（出どころの記録）と比べる。

    **ほかの学生と同じ内容の貼り付け**（2026-09-25）は、貼り付けの指紋の索引
    （`ide_paste_marks`）で同じコースのほかの学習者を数える。この学習者自身は数えない。
    """
    submitted: dict[str, str] = {}
    hashes = [
        str(event["hash"])
        for event in events
        if event.get("type") == "paste" and event.get("origin") != "internal" and event.get("hash")
    ]
    with console.database.unit_of_work() as uow:
        for event in events:
            if event.get("type") == "submit" and event.get("submission_id"):
                link = uow.ide_links.for_submission(SubmissionId(str(event["submission_id"])))
                if link is not None:
                    submitted[str(link.submission_id)] = link.content_hash
        sharing = uow.ide_activity.learners_sharing(session.course_id, hashes) if hashes else {}
    others = {digest: len(learners - {session.learner_id}) for digest, learners in sharing.items()}
    flags = (
        flag_events(events)
        + submission_mismatches(events, submitted)
        + shared_paste_flags(events, others)
    )
    return sorted(flags, key=lambda flag: flag.t)


def _add(a: ActivitySummary, b: ActivitySummary) -> ActivitySummary:
    """2 つの要約を足す（最大の貼り付けだけは大きい方）。"""
    total = ActivitySummary()
    for item in fields(ActivitySummary):
        left, right = getattr(a, item.name), getattr(b, item.name)
        value = max(left, right) if item.name == "largest_external_paste" else left + right
        setattr(total, item.name, value)
    return total


def _worked(summary: ActivitySummary) -> bool:
    """その問題で手を動かしたか（打鍵・貼り付け・読み込み・実行・提出のどれか）。"""
    return bool(
        summary.edits
        or summary.pastes_external
        or summary.pastes_internal
        or summary.file_loads
        or summary.runs
        or summary.submits
    )


def _tab_tasks(console, events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """タブ番号 → (課題 ID, 課題名)。hello に載った課題版から引く。

    消えた課題は ID のまま出す（記録は課題より長く残りうる）。
    """
    hello = next((e for e in events if e.get("type") == "hello"), None)
    version_ids = [] if hello is None else list(hello.get("tasks") or [])
    tabs: list[tuple[str, str]] = []
    with console.database.unit_of_work() as uow:
        for version_id in version_ids:
            version = uow.tasks.get_version(TaskVersionId(str(version_id)))
            task = None if version is None else uow.tasks.get_task(version.task_id)
            if task is None:
                tabs.append((str(version_id), str(version_id)))
            else:
                tabs.append((str(task.id), task.title))
    return tabs


@dataclass
class SessionPart:
    """1 回のセッションのうち、1 つの問題の分。"""

    session: IdeSession
    tab: int
    summary: ActivitySummary
    integrity: IntegrityReport
    unreadable: int


@dataclass
class ProblemView:
    """1 つの問題について、この学習者の作業と提出。"""

    task_id: str
    title: str
    summary: ActivitySummary = field(default_factory=ActivitySummary)
    parts: list[SessionPart] = field(default_factory=list)
    flags: list[tuple[IdeSession, Flag]] = field(default_factory=list)
    submissions: list[dict[str, Any]] = field(default_factory=list)


def _submissions(console, course, learner_id: UserId) -> dict[str, dict[str, Any]]:
    """この学習者の提出。提出 ID → 回数・得点・採用・出どころ・確認画面の場所。

    採用は提出一覧と**同じ規則**（`adopted_ids`）で決める ── 画面によって
    採用が違えば、教員は問い合わせに答えられない。
    """
    with console.database.unit_of_work() as uow:
        rows = uow.submissions.scored_for_course(course.id, learner_ids=[learner_id])
        adopted = adopted_ids(rows, version_max_scores(uow, rows))
        numbers = attempt_ordinals(
            (row.submission_id, row.learner_id, row.task_id, row.submitted_at, row.attempt)
            for row in rows
        )
        origins = {
            str(row.submission_id): link.origin.value
            for row in rows
            if (link := uow.ide_links.for_submission(row.submission_id)) is not None
        }
    return {
        str(row.submission_id): {
            "id": str(row.submission_id),
            "task_id": str(row.task_id),
            # 課題の中での回数（版をまたぐ）。`row.attempt` は版ごとの番号。
            "attempt": numbers.get(row.submission_id, row.attempt),
            "ratio": row.final_ratio,
            "adopted": row.submission_id in adopted,
            "origin": origins.get(str(row.submission_id)),
            "submitted_at": row.submitted_at,
            "href": f"/review/{row.submission_id}/reveal",
        }
        for row in rows
    }


def register(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _instructor(request: Request, course_id: str):
        # 管理画面と同じ「教員だけ」の判定を使う（TA には開けない）。
        from .app import require_principal
        from .manage import _require_instructor

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return me, course

    def _audit(request: Request, me, course_id: str, target: str, what: str, detail: dict):
        console = request.app.state.aijudge
        with console.database.unit_of_work() as uow:
            recorder_for(uow, request, me, role="instructor").record(
                AuditAction.ACTIVITY_VIEWED,
                target_type="learner" if ":" in target else "course",
                target_id=target,
                summary=f"エディタの作業記録を見た（{what}）",
                detail={"course_id": course_id, **detail},
            )
            uow.commit()

    @router.get("/courses/{course_id}/activity", response_class=HTMLResponse)
    def course_activity(request: Request, course_id: str) -> HTMLResponse:
        """コース全体の一覧。**索引だけで作る**（150 名分の本体を読まない）。

        受講者全員を並べる ── 提出していない学習者の記録にも、ここから辿れる。
        """
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        rows = []
        with console.database.unit_of_work() as uow:
            enrolments = [
                e for e in uow.identity.list_enrollments(course.id) if e.role is Role.LEARNER
            ]
            for enrolment in enrolments:
                user = uow.identity.get_user(enrolment.user_id)
                sessions = uow.ide_activity.sessions_for(enrolment.user_id, course.id)
                batches = [uow.ide_activity.batches(s.id) for s in sessions]
                rows.append(
                    {
                        "user_id": str(enrolment.user_id),
                        "login": user.login if user else str(enrolment.user_id),
                        "name": user.display_name if user else "",
                        "sessions": len(sessions),
                        "last": max((s.started_at for s in sessions), default=None),
                        "missing": sum(_missing(b) for b in batches),
                    }
                )
        rows.sort(key=lambda r: (r["sessions"] == 0, r["login"]))
        _audit(request, me, str(course.id), str(course.id), "コースの一覧", {})
        return templates.TemplateResponse(
            request,
            "activity_course.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "作業の記録", "href": f"/courses/{course.id}/activity"},
                "rows": rows,
                "with_record": sum(1 for r in rows if r["sessions"]),
            },
        )

    @router.get("/courses/{course_id}/activity/{learner_id}", response_class=HTMLResponse)
    def learner_activity(request: Request, course_id: str, learner_id: str) -> HTMLResponse:
        """この学習者のエディタでの作業を、**問題ごとに**並べる。"""
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        root = activity_root()
        with console.database.unit_of_work() as uow:
            learner = uow.identity.get_user(UserId(learner_id))
            enrolled = uow.identity.find_enrollment(course.id, UserId(learner_id))
            sessions = uow.ide_activity.sessions_for(UserId(learner_id), course.id)
            indexed = {s.id: uow.ide_activity.batches(s.id) for s in sessions}
            titles = {str(t.id): t.title for t in uow.tasks.list_for_course(course.id)}
        # 受講していない人の記録は、このコースの画面からは見せない。
        if learner is None or enrolled is None:
            raise HTTPException(status_code=404, detail="学習者が見つかりません")

        problems: dict[str, ProblemView] = {}

        def problem(task_id: str, title: str) -> ProblemView:
            if task_id not in problems:
                problems[task_id] = ProblemView(task_id=task_id, title=title)
            return problems[task_id]

        if root is not None:
            files = ActivityFiles(root)
            for session in sessions:
                loaded, unreadable = _read(files, session, indexed[session.id])
                events = [e for _b, batch_events in loaded for e in batch_events]
                integrity = check_session(
                    loaded, lambda name, s=session: files.read_snapshot(s, name)
                )
                tabs = _tab_tasks(console, events)
                flags = _flags(console, events, session)
                for tab, summary in summarize_by_tab(events).items():
                    # **その問題で何もしていない回は並べない。** 開いただけの回や、
                    # 切り替えずに離れた回でも、ハートビートは開いていたタブに
                    # 振り分けられる ── 並べると「0 字・なし」の行が埋め尽くす。
                    if not _worked(summary):
                        continue
                    task_id, title = (
                        tabs[tab] if tab < len(tabs) else (UNKNOWN_TASK, "問題が分からない記録")
                    )
                    view = problem(task_id, title)
                    view.summary = _add(view.summary, summary)
                    view.parts.append(SessionPart(session, tab, summary, integrity, unreadable))
                    view.flags.extend((session, f) for f in flags if f.tab == tab)

        # 提出は、作業の記録が無い問題（ファイルで出した）の分も並べる。
        for row in _submissions(console, course, UserId(learner_id)).values():
            problem(row["task_id"], titles.get(row["task_id"], row["task_id"])).submissions.append(
                row
            )
        for view in problems.values():
            # 提出日時の順。回数は課題の版ごとに数えるので、版が上がると 1 から
            # 数え直す ── 回数で並べると 1・1・2・2 のように混ざる。
            view.submissions.sort(key=lambda r: r["submitted_at"])

        _audit(
            request,
            me,
            str(course.id),
            f"{course.id}:{learner_id}",
            "一覧",
            {"sessions": len(sessions)},
        )
        return templates.TemplateResponse(
            request,
            "activity_learner.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "作業の記録", "href": f"/courses/{course.id}/activity"},
                "learner": learner,
                "problems": sorted(problems.values(), key=lambda p: p.title),
                "configured": root is not None,
                "session_count": len(sessions),
            },
        )

    @router.get(
        "/courses/{course_id}/activity/{learner_id}/{session_id}", response_class=HTMLResponse
    )
    def session_replay(
        request: Request, course_id: str, learner_id: str, session_id: str, tab: int = 0
    ) -> HTMLResponse:
        """1 回のセッションを時刻順に再生する。`tab` は最初に見せる問題。"""
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
        for event in events:
            kind = event.get("type")
            if kind in ("hello", "tabs"):
                wanted.update(h for h in event.get("hashes") or [] if h)
            elif kind == "file_load" and event.get("hash"):
                wanted.add(str(event["hash"]))
        snapshots = {
            name: text
            for name in wanted
            if (text := files.read_snapshot(session, name)) is not None
        }
        tabs = _tab_tasks(console, events)
        integrity = check_session(loaded, lambda name: files.read_snapshot(session, name))
        flags = [
            {"t": flag.t, "label": flag.label, "detail": flag.detail, "tab": flag.tab}
            for flag in _flags(console, events, session)
        ]
        # 提出は「何回目・得点・採用」を添え、確認画面へのリンクを付ける。
        submissions = _submissions(console, course, session.learner_id)
        submitted = {
            str(e["submission_id"]): {
                key: value
                for key, value in submissions[str(e["submission_id"])].items()
                if key in ("attempt", "ratio", "adopted", "href")
            }
            for e in events
            if e.get("type") == "submit" and str(e.get("submission_id")) in submissions
        }
        _audit(
            request,
            me,
            str(course.id),
            f"{course.id}:{learner_id}",
            "再生",
            {"session_id": str(session.id)},
        )
        return templates.TemplateResponse(
            request,
            "activity_session.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "作業の記録", "href": f"/courses/{course.id}/activity"},
                "learner": learner,
                "session": session,
                "integrity": integrity,
                "unreadable": unreadable,
                "flag_count": len(flags),
                # **`<` をすべて逃がす**（`\\u003c`、JSON としてはそのまま読める）。
                # 学習者のコードに `</script>` があると埋め込みから抜け出せ、
                # `<!--` や `<script` でも HTML の読み取りが崩れる。教員の画面で、
                # 教員の権限で動くスクリプトを差し込ませない。
                "replay_json": json.dumps(
                    {
                        "events": events,
                        "snapshots": snapshots,
                        "titles": [title for _task_id, title in tabs],
                        "flags": flags,
                        "submissions": submitted,
                        "tab": tab,
                    },
                    ensure_ascii=False,
                    default=str,
                ).replace("<", "\\u003c"),
            },
        )

    return router
