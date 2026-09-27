"""科目プロファイルの一覧・編集・複製・改名（段階 4-3）。

採点の雛形の管理。**参照されているプロファイルは読み取り専用**で、その判定は
`aijudge_course_admin.profiles` が持つ（#146）。ここは画面と繋ぐだけ。
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_audit import AuditAction
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.profiles import (
    duplicate_profile,
    list_profiles,
    read_profile_text,
    rename_profile,
    save_profile_text,
)
from aijudge_grading import EvaluatorRegistry

from ..audit_context import recorder_for
from ..urls import RedirectResponse
from .common import SUBJECTS_STEP, _console, _require_admin, _trail
from .messages import SAVED_MESSAGES


def _digest(text: str) -> str:
    """プロファイル本文の指紋。

    全文は記録しない ── 数十 KB あり、その大半は「なぜこの値なのか」を書いた
    コメントである。**どの版だったかが後から言えれば足りる。**
    """
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _record_profile_change(console, request, me, *, action, name, summary, detail) -> None:
    """科目プロファイルの変更を監査に残す（ADR 0016）。

    **これは書き込みの後である。** プロファイルはファイルで、DB の
    トランザクションには載らない ── 監査行が書けなくてもファイルは既に
    変わっている。DB の中の操作（成績・権限）が持つ「記録できなければ操作も
    成立しない」という保証は、ここには無い。

    それでも記録する価値はある。プロファイルは採点の設定そのもので、
    **1 つが複数のコースの採点を変える**（`subjects/README.md`）ので、
    誰がいつ触ったかが分からないのは困る。
    """
    with console.database.unit_of_work() as uow:
        recorder_for(uow, request, me).record(
            action,
            target_type="subject_profile",
            target_id=name,
            summary=summary,
            detail=detail,
        )
        uow.commit()


def _used_by(request: Request, names: list[str]) -> dict[str, tuple]:
    """名前ごとの参照コース。**テナントを越えて調べる**（profiles.py 参照）。"""
    console = _console(request)
    with console.database.unit_of_work() as uow:
        return {name: uow.identity.list_courses_using_profile(name) for name in names}


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    # -- 科目プロファイル（管理者専用、#146）--------------------------------
    #
    # **参照されているプロファイルは読み取り専用のまま。** 1 つのプロファイルは
    # 複数のコースの雛形になりうるので、書き換えると自分が担当していない
    # コースの採点まで変わる（ADR 0002 の「コードと同じ扱いでレビューを通す」）。
    #
    # 未参照のものだけ直接編集・改名でき、参照中のものへの唯一の操作は
    # 「複製して編集」。この判定は `aijudge_course_admin.profiles` が持ち、ここは
    # 画面と繋ぐだけ ── 判定を画面側に写すと、2 つが食い違ったときに
    # 「画面では編集できるのに保存が拒否される」形で現れる。

    @router.get("/subjects", response_class=HTMLResponse)
    def subject_list(request: Request, saved: str = "") -> Response:
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        names = [path.stem for path in sorted(console.profiles_dir.glob("*.yaml"))]
        profiles = list_profiles(console.profiles_dir, _used_by(request, names))
        return templates.TemplateResponse(
            request,
            "manage_subjects.html",
            {
                "me": me,
                "trail": _trail(("科目プロファイル", None)),
                "profiles": profiles,
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.get("/subjects/{name}", response_class=HTMLResponse)
    def subject_detail(request: Request, name: str, saved: str = "") -> Response:
        """1 件の全文。**参照中なら読み取り専用で、参照コースをその場に出す。**

        単に灰色にするだけでは、教員は「バグか」「権限が無いだけか」を
        判別できない（#146）。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            text = read_profile_text(name, console.profiles_dir)
        except AdminError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        used_by = _used_by(request, [name])[name]
        return templates.TemplateResponse(
            request,
            "manage_subject.html",
            {
                "me": me,
                "trail": _trail(SUBJECTS_STEP, (name, None)),
                "name": name,
                "text": text,
                "used_by": used_by,
                "editable": not used_by,
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.post("/subjects/{name}", response_class=HTMLResponse)
    async def save_subject(request: Request, name: str) -> Response:
        """全文を保存する。**参照中なら `AdminError` で拒否される**（profiles.py）。"""
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        form = await request.form()
        text = str(form.get("text") or "")
        used_by = _used_by(request, [name])[name]
        try:
            save_profile_text(
                name,
                text,
                profiles_dir=console.profiles_dir,
                registry=EvaluatorRegistry().load_installed(),
                used_by=used_by,
            )
        except AdminError as exc:
            # **書きかけを捨てない。** 直して出し直せるように、送られてきた
            # 全文をそのまま返す（検証エラーで消えると打ち直しになる）。
            return templates.TemplateResponse(
                request,
                "manage_subject.html",
                {
                    "me": me,
                    "trail": _trail(SUBJECTS_STEP, (name, None)),
                    "name": name,
                    "text": text,
                    "used_by": used_by,
                    "editable": not used_by,
                    "error": str(exc),
                },
                status_code=400,
            )
        _record_profile_change(
            console,
            request,
            me,
            action=AuditAction.PROFILE_UPDATED,
            name=name,
            summary=f"科目プロファイル {name} を書き換えた",
            detail={"sha256": _digest(text), "bytes": len(text.encode())},
        )
        return RedirectResponse(
            f"/manage/subjects/{name}?saved=profile_saved#saved", status_code=303
        )

    @router.post("/subjects/{name}/duplicate")
    def duplicate_subject(
        request: Request,
        name: str,
        new_name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """複製して、複製先の編集画面へ渡す（#146 の「複製して編集」）。"""
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            duplicate_profile(
                name,
                new_name.strip(),
                profiles_dir=console.profiles_dir,
                description=description.strip() or None,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_profile_change(
            console,
            request,
            me,
            action=AuditAction.PROFILE_DUPLICATED,
            name=new_name.strip(),
            summary=f"科目プロファイル {name} を {new_name.strip()} に複製した",
            detail={"source": name},
        )
        return RedirectResponse(
            f"/manage/subjects/{new_name.strip()}?saved=profile_duplicated#saved", status_code=303
        )

    @router.post("/subjects/{name}/rename")
    def rename_subject(request: Request, name: str, new_name: Annotated[str, Form()]) -> Response:
        """改名する。**未参照のときだけ**（参照中は採点が止まるので拒否）。"""
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            rename_profile(
                name,
                new_name.strip(),
                profiles_dir=console.profiles_dir,
                used_by=_used_by(request, [name])[name],
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_profile_change(
            console,
            request,
            me,
            action=AuditAction.PROFILE_UPDATED,
            name=new_name.strip(),
            summary=f"科目プロファイル {name} を {new_name.strip()} に改名した",
            detail={"renamed_from": name},
        )
        return RedirectResponse(
            f"/manage/subjects/{new_name.strip()}?saved=profile_renamed#saved", status_code=303
        )
