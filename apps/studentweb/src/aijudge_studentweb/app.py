"""学習者向け Web アプリ。

提出して、結果を見る。それだけ。

**見せる範囲は `visibility.py` が決める。** テンプレートの条件分岐に散らすと、
画面を 1 つ足したときに漏れる（漏れる方向は必ず「確定前の AI 判定が見える」
側で、それは設計原則 P5 を壊す）。

権限はコースの受講で決まる。**URL を推測されても他人の提出は見えない。**
UI で隠すのは表示の都合であって権限ではないので、リクエストごとに
受講と所有者を確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aijudge_authoring import render_statement
from aijudge_core import ArtifactKind, Course, Submission, TaskVersion
from aijudge_core.ids import CourseId, SubmissionId, TaskVersionId
from aijudge_identity import (
    AuthenticationFailed,
    AuthService,
    PermissionDenied,
    Principal,
)
from aijudge_persistence import Database
from aijudge_submission import (
    ArtifactStore,
    IncomingFile,
    SubmissionRejected,
    SubmissionService,
)

from .visibility import ResultView, build_result_view

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE = "aijudge_session"
# 提出できる拡張子。増やすときは採点側（評価器）が扱えることを確かめてから。
SUFFIX_KINDS: dict[str, ArtifactKind] = {
    ".c": ArtifactKind.CODE,
    ".py": ArtifactKind.CODE,
    ".java": ArtifactKind.CODE,
    ".tex": ArtifactKind.LATEX,
    ".md": ArtifactKind.MARKDOWN,
}
# 1 ファイルの上限。学生のコードにこれを超えるものは無く、超えるなら
# 事故か攻撃なので受け付ける前に止める。
MAX_UPLOAD_BYTES = 1 * 1024 * 1024


class StudentApp:
    """アプリの状態。DB とストアを持つ。"""

    def __init__(
        self,
        database: Database,
        artifact_store: ArtifactStore,
        *,
        profiles_dir: Path,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self.database = database
        self.store = artifact_store
        self.profiles_dir = profiles_dir
        self.max_upload_bytes = max_upload_bytes
        self.submissions = SubmissionService(database.unit_of_work, artifact_store)


def _state(request: Request) -> StudentApp:
    return request.app.state.aijudge  # type: ignore[no-any-return]


def current_principal(request: Request) -> Principal | None:
    """Cookie のセッションから主体を引く。無ければ None。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    with _state(request).database.unit_of_work() as uow:
        return AuthService(uow.identity).resolve(token)


def require_principal(request: Request) -> Principal:
    principal = current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="ログインしてください")
    return principal


# 依存はモジュール階層に置く。`create_app` 内のローカル別名にすると、
# FastAPI が注釈を解決できず（`from __future__ import annotations` で
# 文字列になるため）、リクエストボディとして扱われて 422 になる。
Me = Annotated[Principal, Depends(require_principal)]


def create_app(app_state: StudentApp) -> FastAPI:
    app = FastAPI(title="aiJudge")
    app.state.aijudge = app_state

    # -- ログイン ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(
        request: Request,
        login: Annotated[str, Form()],
        password: Annotated[str, Form()],
        tenant: Annotated[str, Form()] = "",
    ) -> Response:
        with app_state.database.unit_of_work() as uow:
            service = AuthService(uow.identity)
            try:
                _, token = service.login(tenant_id=_tenant(tenant), login=login, password=password)
            except AuthenticationFailed as exc:
                # 理由を分けない。有効な ID の一覧を作れてしまう。
                return TEMPLATES.TemplateResponse(
                    request, "login.html", {"error": str(exc)}, status_code=401
                )
            uow.commit()

        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,  # JavaScript から読めない
            samesite="lax",  # 他サイトからの POST でセッションを使わせない
            secure=False,  # HTTPS 化までは False。TLS 終端の前に True にする
            path="/",
        )
        return response

    @app.post("/logout")
    def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            with app_state.database.unit_of_work() as uow:
                AuthService(uow.identity).logout(token)
                uow.commit()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- コースと課題 ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        principal = current_principal(request)
        if principal is None:
            return RedirectResponse("/login", status_code=303)
        with app_state.database.unit_of_work() as uow:
            courses = AuthService(uow.identity).courses_for(principal.tenant_id, principal.user_id)
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"me": principal, "courses": courses}
        )

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course(request: Request, course_id: str, me: Me) -> HTMLResponse:
        course_obj, tasks = _course_and_tasks(app_state, me, CourseId(course_id))
        return TEMPLATES.TemplateResponse(
            request, "course.html", {"me": me, "course": course_obj, "tasks": tasks}
        )

    @app.get("/tasks/{task_version_id}", response_class=HTMLResponse)
    def task(request: Request, task_version_id: str, me: Me) -> HTMLResponse:
        version, course_obj = _task_and_course(app_state, me, TaskVersionId(task_version_id))
        with app_state.database.unit_of_work() as uow:
            submissions = uow.submissions.list_for_learner(me.tenant_id, me.user_id, version.id)
        return TEMPLATES.TemplateResponse(
            request,
            "task.html",
            {
                "me": me,
                "task": version,
                "course": course_obj,
                "submissions": tuple(reversed(submissions)),
                "accepts": sorted(SUFFIX_KINDS),
                # 課題文は Markdown。生のまま出すと `##` や ``` が見える。
                "statement_html": render_statement(version.statement),
            },
        )

    # -- 提出 --------------------------------------------------------------

    @app.post("/tasks/{task_version_id}/submit")
    async def submit(
        request: Request,
        task_version_id: str,
        me: Me,
        upload: UploadFile,
    ) -> Response:
        version, course_obj = _task_and_course(app_state, me, TaskVersionId(task_version_id))
        payload = await upload.read()

        filename = Path(upload.filename or "submission").name
        kind = SUFFIX_KINDS.get(Path(filename).suffix.lower())
        if kind is None:
            raise HTTPException(
                status_code=400,
                detail=f"この形式は提出できません（受付: {', '.join(sorted(SUFFIX_KINDS))}）",
            )
        if not payload:
            raise HTTPException(status_code=400, detail="ファイルが空です")
        if len(payload) > app_state.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"ファイルが大きすぎます（上限 {app_state.max_upload_bytes} バイト）",
            )

        try:
            result = app_state.submissions.accept(
                tenant_id=me.tenant_id,
                task_version_id=version.id,
                learner_id=me.user_id,
                subject_profile=course_obj.subject_profile,
                files=[IncomingFile(filename=filename, kind=kind, payload=payload)],
            )
        except SubmissionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return RedirectResponse(
            f"/submissions/{result.submission.id}" + ("?again=1" if result.deduplicated else ""),
            status_code=303,
        )

    # -- 結果 --------------------------------------------------------------

    @app.get("/submissions/{submission_id}", response_class=HTMLResponse)
    def submission(request: Request, submission_id: str, me: Me, again: int = 0) -> HTMLResponse:
        target, version, run, view = _submission_view(app_state, me, SubmissionId(submission_id))
        source = _source_of(app_state, target)
        return TEMPLATES.TemplateResponse(
            request,
            "submission.html",
            {
                "me": me,
                "submission": target,
                "task": version,
                "run": run,
                "view": view,
                "lines": _numbered(source),
                "duplicate": bool(again),
            },
        )

    return app


# -- 権限つきの読み出し ------------------------------------------------------
#
# URL を推測されても他人のものが見えないこと。UI で隠すのは表示の都合であって
# 権限ではないので、リクエストごとに受講と所有者を確かめる。


def _tenant(raw: str):
    from aijudge_core.ids import TenantId

    # 単独運用では 1 テナント。Phase 8 でホスト名やパスから決める。
    return TenantId(raw or DEFAULT_TENANT)


DEFAULT_TENANT = "ten_" + "0" * 32


def _course_and_tasks(
    app_state: StudentApp, me: Principal, course_id: CourseId
) -> tuple[Course, tuple]:
    with app_state.database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        try:
            auth.require_membership(course_id, me.user_id)
        except PermissionDenied as exc:
            # 存在しないコースと、受講していないコースを区別しない。
            # 区別すると、どのコースが存在するかを列挙できる。
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        course_obj = uow.identity.get_course(course_id)
        if course_obj is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        tasks = uow.tasks.list_for_course(course_id)
        versions = []
        for task in tasks:
            version = uow.tasks.latest_version(task.id)
            if version is not None:
                versions.append((task, version))
    return course_obj, tuple(versions)


def _task_and_course(
    app_state: StudentApp, me: Principal, task_version_id: TaskVersionId
) -> tuple[TaskVersion, Course]:
    with app_state.database.unit_of_work() as uow:
        version = uow.tasks.get_version(task_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        task = uow.tasks.get_task(version.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        course_obj = uow.identity.get_course(task.course_id)
        auth = AuthService(uow.identity)
        try:
            auth.require_membership(task.course_id, me.user_id)
        except PermissionDenied as exc:
            raise HTTPException(status_code=404, detail="課題が見つかりません") from exc
        if course_obj is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
    return version, course_obj


def _submission_view(
    app_state: StudentApp, me: Principal, submission_id: SubmissionId
) -> tuple[Submission, TaskVersion, object | None, ResultView | None]:
    with app_state.database.unit_of_work() as uow:
        target = uow.submissions.get(submission_id)
        if target is None or target.learner_id != me.user_id:
            # 他人の提出は「無い」と答える。所有者が違うことを伝えると、
            # 提出 ID の存在自体が漏れる。
            raise HTTPException(status_code=404, detail="提出が見つかりません")
        version = uow.tasks.get_version(target.task_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        run = uow.runs.latest_for(submission_id)
        review = None if run is None else uow.reviews.find_review_for_run(run.id)

    view = None if run is None else build_result_view(run, version, review)
    return target, version, run, view


def _source_of(app_state: StudentApp, submission: Submission) -> str:
    for artifact in submission.gradable_artifacts:
        try:
            return app_state.store.get(artifact.storage_key).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - ストアが読めない状況
            return ""
    return ""


def _numbered(source: str) -> list[tuple[int, str]]:
    return list(enumerate(source.replace("\r\n", "\n").split("\n"), 1))


def now() -> datetime:  # pragma: no cover - テンプレート用
    return datetime.now(UTC)
