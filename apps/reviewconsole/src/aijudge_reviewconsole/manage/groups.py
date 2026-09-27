"""受講者のグループと出題先、課題文の画像（段階 4-6）。

グループは名簿の部分集合で、課題の出題先になる（`aijudge_course_admin.groups`）。画像は
課題文に貼るもの。どちらも問題セットとは独立に変わるので、問題セットの画面とは分けた
（地図 `docs/design/manage-split-map.md`）。`manage/__init__.py` の `register()` が、元の
グループのルートがあった位置でここの `register` を呼ぶ（画像のルートも同じ位置に寄せた）。
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_authoring import images
from aijudge_core import MAX_GROUP_NAME_LENGTH, Course
from aijudge_core.ids import CourseId
from aijudge_course_admin import groups as audience
from aijudge_identity import Principal

from ..audit_context import recorder_for
from ..urls import RedirectResponse
from .common import _console, _first_error, _require_instructor
from .messages import SAVED_MESSAGES


def _render_groups(
    templates: Jinja2Templates,
    request: Request,
    me: Principal,
    course: Course,
    *,
    saved: str = "",
    result=None,
    error: str | None = None,
    draft: dict | None = None,
    status_code: int = 200,
) -> Response:
    console = _console(request)
    with console.database.unit_of_work() as uow:
        rows = [
            {
                "summary": summary,
                "members": audience.members_of(uow, summary.group),
            }
            for summary in audience.list_groups(uow, course)
        ]
    return templates.TemplateResponse(
        request,
        "manage_groups.html",
        {
            "me": me,
            "course": course,
            "section": {"label": "出題先の名簿", "href": f"/manage/courses/{course.id}/groups"},
            "rows": rows,
            "result": result,
            "error": error,
            "draft": draft,
            "saved": SAVED_MESSAGES.get(saved),
            "max_name": MAX_GROUP_NAME_LENGTH,
        },
        status_code=status_code,
    )


def _split_logins(text: str) -> list[str]:
    """名簿の入力を login の並びにする。**改行・空白・カンマのどれで区切ってもよい**
    ── 表計算ソフトの列を貼ると改行、メールの宛先を貼るとカンマになる。"""
    return [part for part in re.split(r"[\s,、]+", text) if part]


async def _store_statement_image(request: Request, course, upload: UploadFile, alt: str) -> str:
    """画像をストアに置き、**課題文に貼り付ける 1 行**を返す。

    **受け口は課題の編集画面の 1 つだけ**（#300）。以前は共通設定にも
    フォームの受け口があり、そこで作った 1 行を教員が手で貼る形だったが、
    書きかけの問題文を置いて往復することになる ── 課題の編集は 1 つの
    フォームで、保存するまで何も残らない。
    """
    console = _console(request)
    payload = await upload.read()
    try:
        name = images.new_name(payload, upload.filename or "")
        key = images.storage_key(str(course.id), name)
    except images.ImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 同じ中身なら同じ鍵。貼り直しても増えない。
    if not console.store.exists(key):
        console.store.put(key, payload)
    # 表示幅を書いておく。**縮めずに貼ると写真 1 枚で画面が埋まり**、
    # 課題文の続きが画面外へ出る。幅だけを書くので縦横比は保たれる
    # （`aijudge_authoring.images`）。教員はあとから数字を直せる。
    return images.markdown_for(str(course.id), name, alt, width=images.display_width(payload))


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.post("/courses/{course_id}/images.json")
    async def upload_statement_image_json(
        request: Request,
        course_id: str,
        upload: UploadFile,
        alt: Annotated[str, Form()] = "",
    ) -> Response:
        """課題の編集画面から上げる画像（#64）。**貼り付けまでやる。**

        **課題文に貼る画像の受け口はこれ 1 つである**（#300）。別の画面で
        1 行を出して手で貼らせる形も持っていたが、書きかけの問題文を置いて
        別の画面へ行き、戻ってきて貼る、という往復になる ── その間に編集中の
        内容は失われる（課題の編集は 1 つのフォームで、保存するまで何も
        残らない）。**同じ画面で受け取り、カーソル位置に差し込む。**

        画面を遷移させないので JSON で返す。差し込みは呼び出し側の
        JavaScript（`base.html`）が行う。JavaScript が無い場合はここへ来ない
        ── 課題の編集画面はそう言う（`<noscript>`）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        line = await _store_statement_image(request, course, upload, alt)
        return JSONResponse({"markdown": line})

    @router.get("/courses/{course_id}/images/{name}")
    def statement_image(request: Request, course_id: str, name: str) -> Response:
        """課題文に貼られた画像（教員側）。

        **学習者アプリと同じ経路を持つ**（`/images/...`）。課題文は両方の
        画面に出るので、絶対 URL を埋め込むとどちらかのホスト名が課題文に
        焼き付く。相対パスなら、開いている側が自分で返す。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        try:
            payload = console.store.get(images.storage_key(str(course.id), name))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="画像が見つかりません") from exc
        return Response(
            content=payload,
            media_type=images.content_type(name),
            headers=images.response_headers(),
        )

    @router.get("/courses/{course_id}/groups", response_class=HTMLResponse)
    def groups_page(request: Request, course_id: str, saved: str = "") -> Response:
        """出題先の名簿。**受講者の画面とは別にする** ── 受講は「このコースの
        一員か」、名簿は「そのうち誰に出すか」で、別の問いである。"""
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _render_groups(templates, request, me, course, saved=saved)

    @router.post("/courses/{course_id}/groups")
    def save_group(
        request: Request,
        course_id: str,
        name: Annotated[str, Form()] = "",
        members: Annotated[str, Form()] = "",
    ) -> Response:
        """名簿を作る・丸ごと置き換える。1 行に 1 つの login（空白・カンマ区切りも可）。

        **知らない login が 1 つでもあれば何も保存しない。** 画面は入力を残した
        まま、どれが知らない login かを言って突き返す ── 一部だけ登録されると、
        漏れた学生に気づけない。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        logins = _split_logins(members)
        with console.database.unit_of_work() as uow:
            try:
                result = audience.replace_group_members(
                    uow, recorder_for(uow, request, me), course=course, name=name, logins=logins
                )
            except audience.UnknownLogins as exc:
                return _render_groups(
                    templates,
                    request,
                    me,
                    course,
                    error=str(exc),
                    draft={"name": name, "members": members, "unknown": exc.logins},
                    status_code=400,
                )
            except ValidationError as exc:
                # 名前が空・長すぎる（`CourseGroup` の検証）。
                return _render_groups(
                    templates,
                    request,
                    me,
                    course,
                    error=f"グループ名を確かめてください（{_first_error(exc)}）",
                    draft={"name": name, "members": members, "unknown": ()},
                    status_code=400,
                )
            uow.commit()
        return _render_groups(templates, request, me, course, result=result)

    @router.post("/courses/{course_id}/groups/delete")
    def remove_group(
        request: Request, course_id: str, name: Annotated[str, Form()] = ""
    ) -> Response:
        """名簿を消す。**出題先として使われていれば消さない**（先に出題先から外す）。"""
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            try:
                audience.delete_group(uow, recorder_for(uow, request, me), course=course, name=name)
            except audience.GroupError as exc:
                return _render_groups(
                    templates, request, me, course, error=str(exc), status_code=409
                )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/groups?saved=group_deleted", status_code=303
        )
