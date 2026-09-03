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

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aijudge_authoring import images, render_statement
from aijudge_core import (
    MIN_JUSTIFICATION_LENGTH,
    ArtifactKind,
    Course,
    GradingPhase,
    ReviewRequest,
    Role,
    Submission,
    SubmissionWindow,
    Task,
    TaskVersion,
    allowed_suffixes,
    content_type_for,
    grace_minutes,
    kind_for,
)
from aijudge_core.ids import (
    CourseId,
    ReviewRequestId,
    SubmissionId,
    TaskVersionId,
    UserId,
    new_id,
)
from aijudge_identity import (
    AuthenticationFailed,
    AuthService,
    PermissionDenied,
    Principal,
    session_cookie_kwargs,
)
from aijudge_persistence import Database
from aijudge_submission import (
    ArtifactStore,
    IncomingFile,
    SubmissionRejected,
    SubmissionService,
)

from .progress import EMPTY, load_progress
from .visibility import ResultView, build_result_view

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE = "aijudge_session"
# 提出できる拡張子は `aijudge_core.uploads` が持つ。**ここに表を作らない** ──
# 画面が受け付ける形式と教員が指定した形式がずれると、出せるのに採点が
# 種別を知らない提出が生まれる。
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
        # `Secure` を付けるかは配置で決まる（`aijudge_identity.cookies` 参照）。
        # リバースプロキシの `X-Forwarded-Proto` か
        # `AIJUDGE_SECURE_COOKIES=1` で決める。
        response.set_cookie(
            SESSION_COOKIE,
            token,
            **session_cookie_kwargs(forwarded_proto=request.headers.get("x-forwarded-proto")),
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
        # 提出回数と採用される点を一覧に出す。出さないと、学習者は自分の
        # 到達点を知るのに課題を 1 つずつ開くことになる（`progress.py`）。
        with app_state.database.unit_of_work() as uow:
            progress = load_progress(
                uow,
                tenant_id=me.tenant_id,
                learner_id=me.user_id,
                course=course_obj,
                rows=tasks,
            )
        return TEMPLATES.TemplateResponse(
            request,
            "course.html",
            {
                "me": me,
                "course": course_obj,
                "sections": _group_by_unit(tasks, progress=progress),
                "progress": progress,
                "no_progress": EMPTY,
                **build_context(course_obj),
            },
        )

    @app.get("/tasks/{task_version_id}", response_class=HTMLResponse)
    def task(request: Request, task_version_id: str, me: Me) -> HTMLResponse:
        version, course_obj, task_obj = _task_and_course(
            app_state, me, TaskVersionId(task_version_id)
        )
        accepts = allowed_suffixes(task_obj.accepted_suffixes, course_obj.upload_suffixes)
        with app_state.database.unit_of_work() as uow:
            # 一覧と個別画面で同じ規則の点・状態を出すため、
            # ここも `load_progress` を通す（`progress.py`）。
            progress = load_progress(
                uow,
                tenant_id=me.tenant_id,
                learner_id=me.user_id,
                course=course_obj,
                rows=((task_obj, version),),
            ).get(version.id, EMPTY)
        return TEMPLATES.TemplateResponse(
            request,
            "task.html",
            {
                "me": me,
                "task": version,
                "course": course_obj,
                "progress": progress,
                # 新しい提出を上に出す。直前に出したものを探させない。
                "attempts": tuple(reversed(progress.attempts)),
                "accepts": accepts,
                # 提出開始を過ぎているか。**過ぎるまで受け付けない**
                # （`Task.accepts_submissions_at`）。
                "open_for_submission": task_obj.accepts_submissions_at(now()),
                # 課題文は Markdown。生のまま出すと `##` や ``` が見える。
                "statement_html": render_statement(version.statement),
                # 教員・TA が自分のコースを開いているか（#108）。**出せる**が、
                # 出したものは成績にも統計にもならない。**先に言う** ── 言わずに
                # 除くと、教員は自分の提出が採点一覧に無いことを不具合として
                # 追いかけることになる。
                "submitting_as": _role_in(app_state, course_obj.id, me.user_id),
                **build_context(course_obj, task_obj, version),
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
        version, course_obj, _task = _task_and_course(app_state, me, TaskVersionId(task_version_id))
        payload = await upload.read()

        # **受付の外では受け取らない。** 画面で隠すだけでは、URL を知って
        # いれば出せてしまう（隠すのは表示の都合であって制限ではない）。
        #
        # 断る理由を分ける（#73）。「まだ」と「もう」を同じ文言にすると、
        # 学習者は待てば出せるのか、間に合わなかったのかが分からない。
        window = _task.submission_window_at(now())
        if window is SubmissionWindow.NOT_OPEN:
            opens = _task.submissions_open_at or _task.opens_at
            raise HTTPException(
                status_code=409,
                detail=f"まだ提出できません（{opens.strftime('%Y-%m-%d %H:%M')} から受け付けます）",
            )
        if window is SubmissionWindow.CLOSED:
            raise HTTPException(
                status_code=409,
                detail=(
                    "提出の受付は終了しました"
                    f"（{_task.accepts_until.strftime('%Y-%m-%d %H:%M')} まででした）"
                ),
            )

        accepts = allowed_suffixes(_task.accepted_suffixes, course_obj.upload_suffixes)
        filename = Path(upload.filename or "submission").name
        suffix = Path(filename).suffix.lower()
        kind = kind_for(suffix) if suffix in accepts else None
        if kind is None:
            raise HTTPException(
                status_code=400,
                detail=f"この形式は提出できません（受付: {', '.join(accepts)}）",
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
                # 試験の問題セットでは、採点はここでは走らせない（#67）。
                # テスト実行の結果は「どのケースで落ちたか」を含むので、
                # **試験中の学習者にとっては答えの一部**である。
                grading_starts_at=_task.grading_starts_at,
                # 出した人のそのときの役割（#108）。教員・TA の提出は採点まで
                # 通すが、成績にも測定にも数えない。**ここで焼き付ける** ──
                # 測定時に現在の受講から引くと、学生が TA になった瞬間に
                # 過去の提出が測定から消える（ADR 0013 と同じ罠）。
                submitted_as=_role_in(app_state, course_obj.id, me.user_id),
            )
        except SubmissionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return RedirectResponse(
            f"/submissions/{result.submission.id}" + ("?again=1" if result.deduplicated else ""),
            status_code=303,
        )

    @app.get("/images/{course_id}/{name}")
    def statement_image(course_id: str, name: str, me: Me) -> Response:
        """課題文に貼られた画像を返す。**受講者だけ。**

        課題文そのものが受講者にしか出ないので、そこに貼られた画像も同じ
        範囲で足りる。教員コンソールにも同じ経路がある ── 課題文は両方の
        画面に出るので、絶対 URL を埋め込むとどちらかのホスト名が課題文に
        焼き付く（`aijudge_authoring.images`）。
        """
        with app_state.database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            try:
                auth.require_membership(CourseId(course_id), me.user_id)
            except PermissionDenied:
                # 存在と権限を区別しない（コース ID の存在自体を漏らさない）。
                raise HTTPException(status_code=404, detail="画像が見つかりません") from None
        try:
            payload = app_state.store.get(images.storage_key(course_id, name))
        except (images.ImageError, Exception) as exc:
            raise HTTPException(status_code=404, detail="画像が見つかりません") from exc
        return Response(
            content=payload,
            media_type=images.content_type(name),
            # 中身から名前を導いているので、同じ URL の中身は変わらない。
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # -- 結果 --------------------------------------------------------------

    @app.get("/submissions/{submission_id}", response_class=HTMLResponse)
    def submission(request: Request, submission_id: str, me: Me, again: int = 0) -> HTMLResponse:
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        source = _source_of(app_state, loaded.submission)
        return TEMPLATES.TemplateResponse(
            request,
            "submission.html",
            {
                "me": me,
                "submission": loaded.submission,
                "task": loaded.version,
                "run": loaded.run,
                "view": loaded.view,
                "lines": _numbered(source),
                # 提出物そのもの（#75）。**種別で描き分ける** ── 以前は
                # 何でもテキストとして出しており、画像は化けたバイナリに
                # なっていた。
                "files": _submitted_files(loaded.submission),
                "duplicate": bool(again),
                "awaiting_deterministic": loaded.awaiting_deterministic,
                "awaiting_ai": loaded.awaiting_ai,
                # 試験の問題セットか（#67）。**時刻は出さない** ── 教員は
                # 途中で採点を流せるし、試験は延長されうるので、時刻を
                # 約束すると両方向に食い違う。
                "grading_held": loaded.grading_held,
                "grading_in_progress": loaded.grading_in_progress,
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                **build_context(loaded.course, loaded.task, loaded.version, loaded.submission),
            },
        )

    @app.get("/submissions/{submission_id}/state")
    def submission_state(submission_id: str, me: Me) -> JSONResponse:
        """採点が動いているかだけを返す。**画面の代わりではない。**

        結果そのものは返さない ── 返すと、点を出す判断（保留・遅延減点・
        確定の出所）が画面とこことで二重になり、片方だけ直る日が来る
        （`visibility.py` が判断を 1 か所に集めている理由と同じ）。
        ここが答えるのは「まだ動いているか」だけで、変わったら画面を
        取り直させる。

        `no-store` を付ける。中間のキャッシュに拾われると、終わったのに
        「動いています」を返し続ける。
        """
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        return JSONResponse(
            {
                # **試験中は「動いている」と言わない。** 言うと画面が
                # 問い合わせ続ける（#67）。
                "working": loaded.grading_in_progress and not loaded.grading_held,
                "phase": (
                    "deterministic"
                    if loaded.awaiting_deterministic
                    else "ai"
                    if loaded.awaiting_ai
                    else None
                ),
                "graded": loaded.run is not None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/submissions/{submission_id}/artifacts/{artifact_id}")
    def submitted_file(submission_id: str, artifact_id: str, me: Me) -> Response:
        """提出したファイルそのものを返す（#75）。

        **見せてよいのは本人だけ。** `_submission_view` が既にその判定を
        持っている（他人の提出は 404 にして、存在自体を漏らさない）ので、
        そこを通す。課題文の画像（#64）と形は近いが、**見せる相手が違う。**

        `Content-Type` は拡張子から引く。分からなければ
        `application/octet-stream` で返し、ブラウザに解釈させない
        （学習者が出したファイルをそのまま返す経路である）。
        """
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        artifact = next((a for a in loaded.submission.artifacts if str(a.id) == artifact_id), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail="提出物が見つかりません")
        try:
            payload = app_state.store.get(artifact.storage_key)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="提出物が見つかりません") from exc
        return Response(
            content=payload,
            media_type=content_type_for(artifact.filename),
            headers={
                "Cache-Control": "private, max-age=300",
                # **画面に埋め込むのは画像と PDF だけ。** それ以外を
                # インラインにすると、ブラウザが中身を解釈しうる。
                "Content-Disposition": (
                    "inline" if artifact.kind in _INLINE_KINDS else "attachment"
                )
                + f'; filename="{artifact.filename or artifact_id}"',
                # 提出物は学習者が出したファイルである。
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/submissions/{submission_id}/request-review")
    def request_review(
        submission_id: str,
        me: Me,
        reason: Annotated[str, Form()] = "",
    ) -> Response:
        """再確認を依頼する。**根拠説明が必須。**

        項目が空でも欠けていても、同じ 400 と同じ案内を返す。422 を返すと
        学習者には何をすればよいか分からない。

        「納得できない」だけの依頼を受け付けると、教員は何を確認すべきか
        分からないまま全件を見ることになり、導線が機能しなくなる。
        """
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        if loaded.run is None:
            raise HTTPException(status_code=409, detail="まだ採点されていません")
        if loaded.view is not None and not loaded.view.can_request_review:
            raise HTTPException(
                status_code=409,
                detail=loaded.view.request_reason or "この採点には依頼を出せません",
            )

        text = reason.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"どの観点のどこが違うと考えるかを {MIN_JUSTIFICATION_LENGTH} "
                    "文字以上で書いてください"
                ),
            )

        with app_state.database.unit_of_work() as uow:
            try:
                uow.reviews.save_request(
                    ReviewRequest(
                        id=ReviewRequestId(new_id("rrq")),
                        submission_id=loaded.submission.id,
                        grading_run_id=loaded.run.id,
                        learner_id=me.user_id,
                        reason=text,
                        requested_at=datetime.now(UTC),
                    )
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            uow.commit()
        return RedirectResponse(f"/submissions/{submission_id}", status_code=303)

    return app


class SetState(StrEnum):
    """学習者から見た問題セットの段階。**分けて並べる。**

    課題が数十件になると、平らな一覧では「いま出せるのはどれか」が
    読み取れない。学習者が最初に知りたいのはそれである。
    """

    # 提出開始まで待つ。課題文は読める。
    ANNOUNCED = "announced"
    # いま出せる。減点なし。
    OPEN = "open"
    # 締切を過ぎたが、まだ出せる。**出したぶんは減点される**（ADR 0013）。
    # 締切と受付終了のあいだがこれで、**この区間があるから締切で閉じない**。
    LATE = "late"
    # 受付終了。もう出せない。
    CLOSED = "closed"


SET_LABELS: dict[SetState, str] = {
    SetState.OPEN: "提出できる問題セット",
    SetState.LATE: "締切を過ぎた問題セット（減点して提出できます）",
    SetState.ANNOUNCED: "公開された問題セット（提出開始前）",
    SetState.CLOSED: "受付を終了した問題セット",
}


def _group_by_unit(
    rows: tuple, *, progress: dict | None = None, now: datetime | None = None
) -> list[dict[str, object]]:
    """課題を問題セットでまとめ、段階ごとに分けて新しい順に並べる。

    1 回の授業で複数問出るので、平らに並べると何回目の分か分からなくなる。
    さらに学期が進むと数十件になるので、**段階で分けたうえで新しい順**に
    出す ── 学習者が最初に知りたいのは「いま出せるのはどれか」である。

    **公開前の問題セットは出さない。** 公開日時を持たせておいて何も
    起きないなら、その日付は嘘になる。日程を入れていない課題（`opens_at`
    が空）は今までどおり出る。
    """
    moment = now or datetime.now(UTC)
    groups: dict[tuple, dict[str, object]] = {}
    for task, version in sorted(rows, key=lambda row: row[0].sort_key):
        if task.opens_at and moment < task.opens_at:
            continue
        key = (task.session, task.unit)
        group = groups.setdefault(
            key,
            {
                "label": task.unit_label,
                "unit": task.unit,
                "session": task.session,
                "opens_at": task.opens_at,
                "submissions_open_at": task.submissions_open_at,
                "due_at": task.due_at,
                "accepts_until": task.accepts_until,
                "tasks": [],
            },
        )
        group["tasks"].append((task, version))
        # まとまりの日程は、その中で最も早い提示・最も遅い締切を代表にする。
        if task.opens_at and (group["opens_at"] is None or task.opens_at < group["opens_at"]):
            group["opens_at"] = task.opens_at
        if task.submissions_open_at and (
            group["submissions_open_at"] is None
            or task.submissions_open_at < group["submissions_open_at"]
        ):
            group["submissions_open_at"] = task.submissions_open_at
        if task.due_at and (group["due_at"] is None or task.due_at > group["due_at"]):
            group["due_at"] = task.due_at
        # 受付終了は**最も遅いもの**を代表にする。早い側を採ると、まだ
        # 出せる課題があるセットを「受付終了」と書くことになる。
        if task.accepts_until and (
            group["accepts_until"] is None or task.accepts_until > group["accepts_until"]
        ):
            group["accepts_until"] = task.accepts_until

    for group in groups.values():
        group["state"] = _set_state(group, moment)
        # **残り秒数はサーバが数える**（#73）。画面が締切と自分の時計を
        # 比べると、時計のずれがそのまま表示のずれになる。締切前は正、
        # 過ぎていれば負（＝経過時間）。
        # 畳んだ見出しで判断できるだけの情報（#93）。**中が見えなくなるので、
        # 開かずに「自分がやることが残っているか」が分かる必要がある。**
        group["task_count"] = len(group["tasks"])
        # **畳んだ見出しで「やることが残っているか」が分かる必要がある。**
        # 中が見えなくなるので、開かないと未提出に気づけないのでは畳む
        # 意味が無い。採点中も出す ── 畳んだ中で採点が進むと、届いたことに
        # 気づけない（#63 の自動更新が効くのは開いている画面だけ）。
        marks = [(progress or {}).get(version.id) for _task, version in group["tasks"]]
        group["unsubmitted"] = sum(1 for m in marks if m is None or not m.count)
        group["grading"] = sum(1 for m in marks if m is not None and m.grading)
        group["seconds_to_due"] = (
            None if group["due_at"] is None else int((group["due_at"] - moment).total_seconds())
        )
        # 並べ替えの基準になる日付。**その段階で意味のある日付を使う** ──
        # 締め切られたセットは締切、これからのセットは提出開始・公開。
        group["sort_at"] = (
            group["due_at"]
            if group["state"] in (SetState.LATE, SetState.CLOSED)
            else group["submissions_open_at"] or group["opens_at"] or group["due_at"]
        )

    ordered: list[dict[str, object]] = []
    # **出せるものが先。** 学習者が最初に知りたいのは「いま出せるのはどれか」で、
    # 締切を過ぎてもまだ出せるものはその次に近い（#73）。
    for state in (SetState.OPEN, SetState.LATE, SetState.ANNOUNCED, SetState.CLOSED):
        members = [group for group in groups.values() if group["state"] is state]
        # 新しい日付順。日付の無いセットは後ろに置く（並べる根拠が無い）。
        members.sort(key=lambda g: (g["sort_at"] is None, g["sort_at"] or MIN_TIME), reverse=True)
        ordered.append({"state": state, "label": SET_LABELS[state], "groups": members})
    return ordered


MIN_TIME = datetime.min.replace(tzinfo=UTC)


def _set_state(group: dict[str, object], now: datetime) -> SetState:
    """**判定は `Task.submission_window_at` と同じ順序で行う。**

    ここと課題ページで違う答えを出すと、一覧では「提出できる」なのに開くと
    出せない、が起きる。
    """
    opens = group["submissions_open_at"] or group["opens_at"]
    if opens is not None and now < opens:
        return SetState.ANNOUNCED
    accepts_until = group["accepts_until"]
    if accepts_until is not None and now > accepts_until:
        return SetState.CLOSED
    due_at = group["due_at"]
    if due_at is not None and now >= due_at:
        return SetState.LATE
    return SetState.OPEN


# -- 権限つきの読み出し ------------------------------------------------------
#
# URL を推測されても他人のものが見えないこと。UI で隠すのは表示の都合であって
# 権限ではないので、リクエストごとに受講と所有者を確かめる。


def _tenant(raw: str):
    from aijudge_core.ids import TenantId

    # 単独運用では 1 テナント。Phase 8 でホスト名やパスから決める。
    return TenantId(raw or DEFAULT_TENANT)


DEFAULT_TENANT = "ten_" + "0" * 32


def _role_in(app_state: StudentApp, course_id: CourseId, user_id: UserId) -> Role:
    """このコースでの役割（#108）。**受講が無ければ学習者として扱う。**

    ここに来る時点で `require_membership` は通っているので、無いのは通常
    起こらない。起きたときに試行扱いにすると、その提出は静かに測定から
    消える ── 数えられて気づくほうがよい。
    """
    with app_state.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(course_id, user_id)
    return Role.LEARNER if enrollment is None else enrollment.role


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
            # 取り下げた課題は出さない（#51）。**消えてはいない** ── 提出も
            # 採点も残っており、教員の一覧には印付きで並ぶ。
            if task.withdrawn:
                continue
            # **承認済みの版だけを出す**（#48）。`latest_version` は版番号
            # だけを見るので、生成したまま誰も見ていない版や却下した版が
            # そのまま学習者に出ていた ── 画面は「未承認 — 出題されません」
            # と書いてある。承認済みが無い課題はまだ存在しないものとして扱う。
            version = uow.tasks.latest_published_version(task.id)
            if version is not None:
                versions.append((task, version))
    return course_obj, tuple(versions)


def _task_and_course(
    app_state: StudentApp, me: Principal, task_version_id: TaskVersionId
) -> tuple[TaskVersion, Course, Task]:
    with app_state.database.unit_of_work() as uow:
        version = uow.tasks.get_version(task_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        if not version.is_published:
            # **一覧から外すだけでは、URL を知っていれば開ける**（提出開始の
            # 判定と同じ理屈）。承認前・却下済みの版は開かせないし、提出も
            # 受け付けない（#48・設計原則 P5）。
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        task = uow.tasks.get_task(version.task_id)
        if task is None or task.withdrawn:
            # 一覧から外すだけでは、URL を知っていれば開ける（#48 と同じ理屈）。
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        course_obj = uow.identity.get_course(task.course_id)
        auth = AuthService(uow.identity)
        try:
            auth.require_membership(task.course_id, me.user_id)
        except PermissionDenied as exc:
            raise HTTPException(status_code=404, detail="課題が見つかりません") from exc
        if course_obj is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
    return version, course_obj, task


@dataclass(frozen=True)
class LoadedSubmission:
    """結果画面が要るもの一式。

    文脈（コース・問題セット・課題・提出）をすべての画面に出すために、まとめて返す。
    """

    submission: Submission
    version: TaskVersion
    task: Task
    course: Course
    run: object | None
    view: ResultView | None
    # 採点キューにまだ仕事が残っているか。**段階ごとに持つ**（ADR 0011）──
    # 決定的評価が届いても AI 評価はこれからで、その区間に何も言わないと
    # 学習者は再読み込みを繰り返すしかない。
    awaiting_deterministic: bool = False
    awaiting_ai: bool = False
    # 採点開始時刻がまだ来ていない（試験・#67）。
    grading_held: bool = False

    @property
    def grading_in_progress(self) -> bool:
        """機械の採点がまだ動いているか。**人の採点待ちは含めない** ──
        押しても届かないものを「待っています」と言うと待ち続けさせる。"""
        return self.awaiting_deterministic or self.awaiting_ai


def _submission_view(
    app_state: StudentApp, me: Principal, submission_id: SubmissionId
) -> LoadedSubmission:
    with app_state.database.unit_of_work() as uow:
        target = uow.submissions.get(submission_id)
        if target is None or target.learner_id != me.user_id:
            # 他人の提出は「無い」と答える。所有者が違うことを伝えると、
            # 提出 ID の存在自体が漏れる。
            raise HTTPException(status_code=404, detail="提出が見つかりません")
        run = uow.runs.latest_for(submission_id)
        # **その採点が使った版で描く。** 提出が指すのは出したときの版だが、
        # 実施中に課題を訂正して採点し直すと、採点はあとの版で付く（#43）。
        # 提出側の版で描くと、観点の重みや段階の説明が採点と食い違う。
        version = uow.tasks.get_version(
            target.task_version_id if run is None else run.context.task_version_id
        )
        if version is None:
            version = uow.tasks.get_version(target.task_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        task = uow.tasks.get_task(version.task_id)
        course = None if task is None else uow.identity.get_course(task.course_id)
        if task is None or course is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        review = None if run is None else uow.reviews.find_review_for_run(run.id)
        request = None if run is None else uow.reviews.find_request_for_run(run.id)
        # 確定は Finalization が表す。HumanReview は「教員が読んだ」記録で
        # あって確定ではない（ADR 0010）。
        finalization = None if run is None else uow.reviews.find_finalization_for_run(run.id)
        awaiting_deterministic = uow.jobs.awaiting(submission_id, GradingPhase.DETERMINISTIC)
        # 待っているのが「順番」なのか「試験の終わり」なのかで、画面に
        # 書くことも自動更新の要否も変わる。
        held = bool(uow.jobs.waiting_count([submission_id], now()))
        awaiting_ai = uow.jobs.awaiting(submission_id, GradingPhase.AI)

    view = (
        None
        if run is None
        else build_result_view(
            run,
            version,
            review,
            request=request,
            finalization=finalization,
            # 仮確定の窓を出すのに要る。**起点は採点完了時刻**（run が持つ）で、
            # 猶予は問題セットかコースが持つ。
            auto_finalize_after_minutes=grace_minutes(
                task.auto_finalize_after_minutes, course.auto_finalize_after_minutes
            ),
        )
    )
    return LoadedSubmission(
        submission=target,
        version=version,
        task=task,
        course=course,
        run=run,
        view=view,
        awaiting_deterministic=awaiting_deterministic,
        awaiting_ai=awaiting_ai,
        grading_held=held,
    )


# 画面に埋め込んでよい種別。**それ以外はダウンロードさせる** ── 学習者が
# 出したファイルをインラインで返すと、ブラウザが中身を解釈しうる（#75）。
_INLINE_KINDS = (ArtifactKind.IMAGE, ArtifactKind.PDF)


def _submitted_files(submission: Submission) -> tuple[dict[str, object], ...]:
    """提出物を画面に出すための行。**種別が出し方を決める。**

    種別は提出時に決まっている（`aijudge_core.uploads.kind_for`）ので、
    ここで判定し直さない。
    """
    return tuple(
        {
            "id": str(artifact.id),
            "filename": artifact.filename or "提出物",
            "kind": artifact.kind,
            "is_image": artifact.kind is ArtifactKind.IMAGE,
            "is_pdf": artifact.kind is ArtifactKind.PDF,
            "byte_size": artifact.byte_size,
        }
        for artifact in submission.gradable_artifacts
    )


def _source_of(app_state: StudentApp, submission: Submission) -> str:
    """提出物を本文として読む。**読めるものだけ。**

    以前は種別を見ずに `decode` していたので、画像や PDF を出すと化けた
    バイナリが行番号つきで並んだ（#75）。読めないものは空を返し、画面は
    種別に応じた出し方（`<img>`・ダウンロード）に切り替える。
    """
    for artifact in submission.gradable_artifacts:
        if artifact.kind in _INLINE_KINDS or artifact.kind is ArtifactKind.DOCX:
            return ""
        try:
            return app_state.store.get(artifact.storage_key).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - ストアが読めない状況
            return ""
    return ""


def build_context(
    course: Course,
    task: Task | None = None,
    version: TaskVersion | None = None,
    submission: Submission | None = None,
) -> dict[str, object]:
    """どのコースのどの問題セットのどの課題か、誰の何回目の提出かを 1 つにまとめる。

    **すべての画面に出す。** 出さないと、複数のコース・問題セット・提出を行き来する
    うちに「いま何を見ているか」が分からなくなる。ブラウザの戻る操作や
    リンクの共有で途中の画面から入ることもある。
    """
    return {
        "ctx_course": course,
        "ctx_task": task,
        "ctx_version": version,
        "ctx_submission": submission,
    }


def _numbered(source: str) -> list[tuple[int, str]]:
    return list(enumerate(source.replace("\r\n", "\n").split("\n"), 1))


def now() -> datetime:  # pragma: no cover - テンプレート用
    return datetime.now(UTC)
