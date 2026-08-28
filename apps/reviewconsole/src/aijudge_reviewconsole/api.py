"""課題を足す API。**非対話の呼び出し元のための入口。**

既存科目の初回移行は、エージェントがディレクトリを読んでここに流し込む。
移行元の形式（Sharif Judge のディレクトリ）を知っているのは呼び出し側で、
**サーバは中立な宣言（`TaskSpec`）しか受け取らない**。移行元の語彙を HTTP に
持ち込むと、移行が終わったあとも一生ついて回る。

認証は `Authorization: Bearer aij_...`。トークンは利用者の権限で動くので、
課題を足せるのはそのコースの INSTRUCTOR 以上に限られる（画面と同じ規則）。

**画面のセッション Cookie では通さない。** 通すと、教員がログインしたまま
別サイトを開いたときに、そのサイトから課題を書き換えられる（CSRF）。
API は Cookie を見ないので、その経路が最初から無い。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from aijudge_admin import AdminError, save_task
from aijudge_authoring import TaskSpec
from aijudge_core import Role
from aijudge_core.ids import CourseId
from aijudge_identity import AuthService, PermissionDenied, Principal

BEARER = "Bearer "


class TaskResponse(BaseModel):
    """保存の結果。**何が起きたかを返す。**

    `created` を返すのは、流し込みを二度走らせたときに気づけるようにするため。
    「保存しました」だけでは 100 件のうち何件が新規か分からない。
    """

    task_id: str
    task_version_id: str
    title: str
    created: bool
    test_cases: int
    auto_graded: bool
    criteria: list[str]


def _console(request: Request):
    return request.app.state.aijudge


def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """`Authorization: Bearer` から主体を引く。

    **Cookie は見ない**（モジュール docstring 参照）。失敗の理由は分けない ──
    「そんなトークンは無い」と「失効している」を分けて返すと、有効な
    トークンを探る手掛かりになる。
    """
    if not authorization or not authorization.startswith(BEARER):
        raise HTTPException(
            status_code=401,
            detail="Authorization: Bearer <token> が要ります",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len(BEARER) :].strip()
    console = _console(request)
    with console.database.unit_of_work() as uow:
        principal = AuthService(uow.identity).resolve_api_token(token)
        # 最終使用日時の記録は書き込みなので、確定させる。
        uow.commit()
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="トークンが無効です",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


Caller = Annotated[Principal, Depends(require_token)]


def register() -> APIRouter:
    """ルータを返す。呼ぶたびに新しく作る（`create_app` を 2 回呼べるように）。"""
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/whoami")
    def whoami(me: Caller) -> dict[str, str]:
        """トークンが誰として通るかを確かめる。流し込み前の疎通確認用。"""
        return {"user_id": str(me.user_id), "login": me.login, "display_name": me.display_name}

    @router.get("/courses")
    def list_courses(request: Request, me: Caller) -> list[dict[str, object]]:
        """このトークンが課題を足せるコース。

        足せないコースは返さない。一覧に出しておいて 403 を返すと、
        呼び出し側は存在するコースの ID を集められる。
        """
        console = _console(request)
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            out = []
            for course in auth.courses_for(me.tenant_id, me.user_id):
                enrollment = uow.identity.find_enrollment(course.id, me.user_id)
                if enrollment is None or enrollment.role not in (Role.INSTRUCTOR, Role.ADMIN):
                    continue
                out.append(
                    {
                        "id": str(course.id),
                        "code": course.code,
                        "title": course.title,
                        "term": course.term,
                        "subject_profile": course.subject_profile,
                    }
                )
        return out

    @router.post("/courses/{course_id}/tasks", status_code=201)
    def create_task(
        request: Request, course_id: str, spec: TaskSpec, me: Caller
    ) -> TaskResponse:
        """課題を 1 件足す。**冪等**（同じ `key` に同じ内容なら増えない）。

        内容が違う場合は 409。過去の採点基準を書き換えないため（P8）で、
        問題文を直したなら版を上げる操作が別に要る。
        """
        console = _console(request)
        course = _require_instructor(console, me, CourseId(course_id))
        try:
            saved = save_task(
                console.database,
                course_id=course.id,
                spec=spec,
                subject_profile=course.subject_profile,
                authored_by=me.user_id,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return TaskResponse(
            task_id=str(saved.task.id),
            task_version_id=str(saved.version.id),
            title=saved.task.title,
            created=saved.created,
            test_cases=saved.test_cases,
            auto_graded=saved.auto_graded,
            criteria=[c.code for c in saved.version.criteria],
        )

    @router.get("/courses/{course_id}/tasks")
    def list_tasks(request: Request, course_id: str, me: Caller) -> list[dict[str, object]]:
        """このコースの課題。流し込みの結果を突き合わせるのに使う。"""
        console = _console(request)
        course = _require_instructor(console, me, CourseId(course_id))
        with console.database.unit_of_work() as uow:
            out = []
            for task in uow.tasks.list_for_course(course.id):
                version = uow.tasks.latest_version(task.id)
                if version is None:
                    continue
                out.append(
                    {
                        "id": str(task.id),
                        "title": task.title,
                        "unit": task.unit,
                        "session": task.session,
                        "position": task.position,
                        "test_cases": len(version.test_cases),
                        "criteria": [c.code for c in version.criteria],
                        "due_at": None if task.due_at is None else task.due_at.isoformat(),
                    }
                )
        return sorted(out, key=lambda row: (row["session"] or 10**6, row["position"] or 10**6))

    return router


def _require_instructor(console, me: Principal, course_id: CourseId):
    """そのコースの教員であること。**TA には開けない**（画面と同じ規則）。"""
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        try:
            role = auth.require_membership(course_id, me.user_id)
        except PermissionDenied as exc:
            # 存在と権限を区別しない（コースを列挙させない）。
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        if role not in (Role.INSTRUCTOR, Role.ADMIN):
            raise HTTPException(status_code=403, detail="この操作には担当教員の権限が必要です")
        course = uow.identity.get_course(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="コースが見つかりません")
    return course


__all__ = ["TaskResponse", "register", "require_token"]
