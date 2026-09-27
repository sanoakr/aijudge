"""コースの知識要素（KC）・語彙・シラバスからの候補（段階 4-5）。

知識要素の画面。何を問うかの体系はここで決め、課題の側から膨らませない（#322）。
名前の修正は担当教員、引退・削除は他のコースからも取り上げる操作なので管理者
（`aijudge_course_admin.kc`）。`manage/__init__.py` の `register()` が、元の KC のルートが
あった位置でここの `register` を呼ぶ（シラバスからの候補 `propose_kcs` も同じ位置に寄せた）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_core.ids import CourseId
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.kc import allowed_namespaces, assert_registered, list_for_namespaces
from aijudge_course_admin.kc import delete as delete_kc
from aijudge_course_admin.kc import edit as edit_kc
from aijudge_course_admin.kc import restore as restore_kc
from aijudge_course_admin.kc import retire as retire_kc
from aijudge_course_admin.kc import usage as kc_usage
from aijudge_course_admin.syllabus import SyllabusReader
from aijudge_grading import load_profile

from ..urls import RedirectResponse
from .common import _console, _is_admin, _require_instructor
from .messages import SAVED_MESSAGES


def _is_component(key: str) -> bool:
    """知識要素そのもの（`名前空間.分野.単位.知識要素`）か。

    分野（`cs.loops`）と単位（`cs.loops.control`）は骨格の枝であって、
    課題が問うものではない。範囲に入れる対象にしない。
    """
    return len(key.split(".")) >= 4


def _kc_page(
    templates: Jinja2Templates,
    request: Request,
    me,
    course,
    *,
    saved: str = "",
    proposal=None,
    discarded: tuple[str, ...] = (),
) -> Response:
    """知識要素のページ。**候補が出ているかどうかだけが違う。**

    候補を別のページにすると、教員は「いま体系に何があるか」を見ずに
    候補を選ぶことになる。重複を作らせないための情報が、選ぶ画面に
    無いことになる。

    `draft` は候補から追加フォームに取り込んだ 1 件（キー・名前・説明）。
    **取り込んだだけでは何も登録されない。** `draft_exists` は、そのキーが
    既に体系にあるか ── あるなら登録は「このコースの範囲に入れる」だけを
    意味し、名前と説明は変わらない。
    """
    console = _console(request)

    profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
    namespaces = allowed_namespaces(profile)
    kcs = list_for_namespaces(console.database, namespaces)
    rows = _kc_rows(console, course, kcs)
    chosen = set(course.knowledge_components)
    # 直前の足す・外すの結果（件数）。画面に出したら消す。
    scope_result = None
    if console.last_kc_scope and console.last_kc_scope[0] == str(course.id):
        _cid, action, changed, kept = console.last_kc_scope
        scope_result = {"action": action, "changed": changed, "kept": kept}
        console.last_kc_scope = None
    return templates.TemplateResponse(
        request,
        "manage_kc.html",
        {
            "me": me,
            "course": course,
            "section": {"label": "知識要素", "href": f"/manage/courses/{course.id}/kc"},
            "namespaces": namespaces,
            "rows": rows,
            "chosen_count": len(chosen),
            "chosen_keys": chosen,
            # 名前空間の語彙を階層ごとに。ここから足す・外す（#289）。
            "groups": _vocabulary_groups(kcs, chosen),
            "scope_result": scope_result,
            "saved": SAVED_MESSAGES.get(saved),
            "saved_key": saved,
            "is_admin": _is_admin(request, me),
            # 候補。**既にあるものは採用させない**ので、突き合わせる鍵を渡す。
            "proposal": proposal,
            # 形が正準キーになっていないので落とした候補（#157）。
            # **減った件数を黙らせない。**
            "discarded": discarded,
            # **「既にある」はこのコースの範囲にあるものを指す。** 語彙に
            # あっても範囲外なら採用できる（採用すれば範囲に入る）。候補は
            # 登録済みの語彙からしか来ない（2026-09-13 決定）ので、状態は
            # 「このコースで使用中」か「語彙にあり（範囲外）」の 2 つ。
            "existing": [row["kc"].key for row in rows if not row["kc"].deprecated],
            "has_basics": bool((course.description or "").strip()),
        },
    )


def _kc_rows(console, course, kcs):
    """一覧の行 ── **このコースが使うもの**と、**このコースの課題が使っているもの**。

    2 つは別物である。範囲から外しても、既に出題した課題の Q-matrix は
    動かない（追記のみ・P8）ので、課題が使っているものは範囲に無くても
    残す ── 消してしまうと、その課題が何を問うているのかを画面から辿る
    手段が無くなる。

    **範囲に無く、どの課題も使っていないものは出さない。** 同じ名前空間を
    複数のコースが共有するので、出し続けると「このコースが使わないと決めた
    もの」が一覧に残り、決めたこと自体が画面から読めなくなる。足すときは
    名前空間の一覧（`_vocabulary_groups`）から。

    **このコースの課題が使っているものは外せない**（#289）。外すと Q-matrix
    が課題の中身と食い違う。理由（課題の件数）を添えて残す。
    """
    chosen = set(course.knowledge_components)
    usage_rows = kc_usage(console.database, kcs)
    here = _kc_use_in_course(console, course)
    kcs = tuple(kc for kc in kcs if kc.key in chosen or here.get(kc.key))
    return [
        {
            "usage": usage_rows[kc.key],
            "kc": kc,
            "used": usage_rows[kc.key].used,
            "tasks": usage_rows[kc.key].tasks,
            "courses": usage_rows[kc.key].courses,
            "in_course": kc.key in chosen,
            "used_here": here.get(kc.key, 0),
            "removable": kc.key in chosen and not here.get(kc.key),
        }
        for kc in kcs
    ]


def _kc_use_in_course(console, course) -> dict[str, int]:
    """**このコースの課題**が使っている知識要素と、その件数。

    `kc_usage` はコースをまたいで数える（引退させてよいかの判断に要る）。
    こちらは「このコースの課題が何を問うているか」で、別の問いである。
    """
    counts: dict[str, int] = {}
    with console.database.unit_of_work() as uow:
        by_id = {str(kc.id): kc.key for kc in uow.skills.list_kcs(None)}
        for task in uow.tasks.list_for_course(course.id):
            version = uow.tasks.latest_version(task.id)
            if version is None:
                continue
            for entry in version.q_matrix:
                key = by_id.get(str(entry.kc_id))
                if key is not None:
                    counts[key] = counts.get(key, 0) + 1
    return counts


def _scope_in(console, course, keys: tuple[str, ...]) -> int:
    """足した知識要素を、このコースが使う範囲にも入れる。**足した数を返す。**

    **「このコースに追加する」は、登録と範囲の両方を意味する。** 片方だけ
    だと、追加しても一覧に出てこない ── 範囲から外したものを戻す道が
    塞がる（外れたものは一覧から隠れるため、戻す道がここになる）。
    """
    before = set(course.knowledge_components)
    merged = tuple(sorted(before | {k for k in keys if k}))
    if merged == tuple(course.knowledge_components):
        return 0
    with console.database.unit_of_work() as uow:
        uow.identity.save_course(course.model_copy(update={"knowledge_components": merged}))
        uow.commit()
    return len(merged) - len(before)


def _scope_targets(console, course, kc: list[str], prefix: str) -> tuple[str, ...]:
    """足す・外す対象のキー。個別のチェックか、階層の接頭辞か。

    接頭辞は `cs.loops` のように**区切りまで一致**させる（`cs.loop` で
    `cs.loops` を巻き込まない）。引退した知識要素は対象にしない。

    **接頭辞が来たらチェックは見ない。** 画面は全分野のチェックを 1 つの
    form に持ち、分野ごとの「この階層をすべて足す／外す」も同じ form の
    送信ボタンなので、押したときに他の分野で付けたチェックも一緒に届く。
    「この階層を」と書いたボタンが別の分野のものを動かしてはいけない。
    """
    prefix = prefix.strip()
    keys = set() if prefix else {key.strip() for key in kc if key.strip()}
    if prefix:
        namespaces = allowed_namespaces(
            load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        )
        for item in list_for_namespaces(console.database, namespaces, include_deprecated=False):
            if _is_component(item.key) and (
                item.key == prefix or item.key.startswith(prefix + ".")
            ):
                keys.add(item.key)
    return tuple(sorted(keys))


def _vocabulary_groups(kcs, chosen: set[str]) -> list[dict]:
    """名前空間の語彙を**階層ごと**にまとめる（#289）。

    987 件を平らに並べても選べない。分野（`cs.loops`）ごとに畳み、その
    階層をまとめて足す・外すための接頭辞と、コースに入っている数を添える。
    引退したものは足せないので出さない。
    """
    labels = {kc.key: kc.label for kc in kcs}
    groups: dict[str, list] = {}
    for kc in kcs:
        if kc.deprecated or not _is_component(kc.key):
            continue
        parts = kc.key.split(".")
        prefix = ".".join(parts[:2])
        groups.setdefault(prefix, []).append(kc)
    return [
        {
            "prefix": prefix,
            "label": labels.get(prefix, ""),
            "kcs": members,
            "total": len(members),
            "in_course": sum(1 for kc in members if kc.key in chosen),
        }
        for prefix, members in sorted(groups.items())
    ]


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.post("/courses/{course_id}/kc/candidates", response_class=HTMLResponse)
    def propose_kcs(request: Request, course_id: str) -> Response:
        """コースの基本情報から知識要素の候補を出す。**登録はしない。**

        **本文を貼り直させない。** 材料はコースが既に持っている
        （`Course.description` ── 基本情報のページでシラバスから読み取って
        保存したもの）。同じ本文をもう一度貼らせると、2 つの経路で入った
        別々のシラバスがコースの中に並ぶことになり、どちらが本当か分からない。

        候補は候補のまま知識要素のページに戻す。**ここから直接は登録しない**
        （`aijudge_course_admin.kc` の規則 4 ── AI には KC を作らせない）。教員が
        1 件ずつ追加フォームに取り込み、確かめてから登録する（`draft_candidate`）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        if not (course.description or "").strip():
            # 材料が無い。**候補を出せないことと、候補が無いことは違う。**
            raise HTTPException(
                status_code=400,
                detail=(
                    "コースの基本情報が空です。先に「基本情報」でシラバスを"
                    "読み取るか、概要・到達目標を書いてください。"
                ),
            )

        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        vocabulary = list_for_namespaces(console.database, namespaces, include_deprecated=False)
        existing = [kc.key for kc in vocabulary]
        # 題名も渡す。「プログラミング及び実習 II」だけで分野が決まることは
        # ないが、本文が到達目標だけのときに科目の見当が付く。
        body = f"# {course.title}\n\n{course.description}"
        try:
            # **候補は登録済みの語彙から選ばれる**（2026-09-13 決定）。一覧に
            # 無いキーは `SyllabusReader` の関門が落とし、理由付きで返る。
            result = SyllabusReader().propose(
                body, namespaces=namespaces, existing_keys=tuple(existing)
            )
        except Exception as exc:  # 生成の失敗は運用の事象。理由を画面に返す。
            raise HTTPException(
                status_code=502,
                detail=f"候補を作れませんでした（S6 が止まっている可能性があります）: {exc}",
            ) from exc
        return _kc_page(
            templates, request, me, course, proposal=result.proposal, discarded=result.discarded
        )

    @router.get("/courses/{course_id}/kc", response_class=HTMLResponse)
    def kc_index(request: Request, course_id: str, saved: str = "") -> Response:
        """このコースが使える KC の一覧。

        **コースをまたいで共有される語彙である。** 同じ名前空間を使う他の
        コースにも同じものが見えるので、どれだけ使われているかを添える。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _kc_page(templates, request, me, course, saved=saved)

    @router.post("/courses/{course_id}/kc/retire")
    def retire_kc_route(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
        superseded_by: Annotated[str, Form()] = "",
        restore: Annotated[str, Form()] = "",
    ) -> Response:
        """KC を引退させる（または引退を取り消す）。**消さない**（P8）。

        引退は管理者のみ。**コースをまたいで効く**操作で、1 コースの教員が
        他のコースの語彙を畳めてはいけない。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if not _is_admin(request, me):
            raise HTTPException(
                status_code=403,
                detail="知識要素の引退には管理者権限が必要です（他のコースにも効きます）",
            )
        console = _console(request)
        try:
            if restore:
                restore_kc(console.database, key=key.strip())
            else:
                retire_kc(
                    console.database,
                    key=key.strip(),
                    superseded_by_key=superseded_by.strip() or None,
                )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved = "kc_restored" if restore else "kc_retired"
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved={saved}#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/adopt")
    async def adopt_candidates(request: Request, course_id: str) -> Response:
        """候補をまとめてこのコースの範囲に入れる（画面の「印を付けた候補をまとめて」）。

        **語彙への登録は行わない**（2026-09-13 決定）。候補は登録済みの語彙から
        選ばれたものだけなので、ここですることは範囲に入れることだけ。万一
        未登録のキーが来たら断る（画面を経ない POST）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        form = await request.form()
        keys = tuple(dict.fromkeys(str(v).strip() for v in form.getlist("adopt") if str(v).strip()))
        if not keys:
            raise HTTPException(status_code=400, detail="採用する候補に印を付けてください")
        try:
            assert_registered(console.database, keys)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        added = _scope_in(console, course, keys)
        console.last_kc_scope = (str(course.id), "added", added, 0)
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_scoped#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/scope/add")
    def add_kc_scope(
        request: Request,
        course_id: str,
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        prefix: Annotated[str, Form()] = "",
    ) -> Response:
        """知識要素をこのコースが使う範囲に足す ── 個別（チェック）にも、
        階層ごと（`prefix`）にも（#289）。

        **語彙への登録ではない。** 名前空間に既にあるものを、このコースの
        作問候補に入れるだけ。無いキーは黙って落とさず断る。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        keys = _scope_targets(console, course, kc, prefix)
        if not keys:
            raise HTTPException(status_code=400, detail="足す知識要素が選ばれていません")
        try:
            assert_registered(console.database, keys)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        added = _scope_in(console, course, keys)
        console.last_kc_scope = (str(course.id), "added", added, 0)
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_scoped#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/scope/remove")
    def remove_kc_scope(
        request: Request,
        course_id: str,
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        prefix: Annotated[str, Form()] = "",
    ) -> Response:
        """知識要素をこのコースが使う範囲から外す（#289）。

        **共有の語彙からの削除ではない。** 外しても知識要素は残り、他のコースの
        Q-matrix は壊れない。**このコースの課題が使っているものは外さない**
        ── 外すと Q-matrix が課題の中身と食い違う。残した数は結果に出す。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        keys = set(_scope_targets(console, course, kc, prefix))
        if not keys:
            raise HTTPException(status_code=400, detail="外す知識要素が選ばれていません")
        here = _kc_use_in_course(console, course)
        kept = {key for key in keys if here.get(key)}
        remaining = tuple(
            key for key in course.knowledge_components if key not in keys or key in kept
        )
        removed = len(course.knowledge_components) - len(remaining)
        if removed:
            with console.database.unit_of_work() as uow:
                uow.identity.save_course(
                    course.model_copy(update={"knowledge_components": remaining})
                )
                uow.commit()
        console.last_kc_scope = (str(course.id), "removed", removed, len(kept))
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_scoped#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/edit")
    def edit_kc_route(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """名前と説明を直す。**キーは直せない。**

        **引退・削除と違って教員が直せる。** あちらは他のコースが使って
        いるものを取り上げる操作なので管理者に限るが、名前を直すのは
        取り上げる操作ではない。正しい名前を知っているのは科目の専門家で
        あり（`aijudge_course_admin.kc` の冒頭）、キーは動かないので壊れない。
        間違えても、もう一度直せる。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        try:
            edit_kc(
                console.database,
                key=key.strip(),
                label=label,
                description=description,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_edited#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/delete")
    def delete_kc_route(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
    ) -> Response:
        """**一度も使われていない知識要素だけを消す。**

        引退（`retire`）は「使っていたが今後は使わない」を表す記録で、
        打ち間違いの置き場所ではない。使われたことの無い `cs.c_langauge` を
        引退させて残すと、コースをまたいで共有される一覧に、誰の役にも
        立たない行が永久に並ぶ。

        使われている KC は消さない ── 判定は `aijudge_course_admin.kc.delete` が
        持つ（利用状況を数えられるのはあちら）。

        引退と同じく管理者のみ。**コースをまたいで効く。**
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if not _is_admin(request, me):
            raise HTTPException(
                status_code=403,
                detail="知識要素の削除には管理者権限が必要です（他のコースにも効きます）",
            )
        console = _console(request)
        try:
            delete_kc(console.database, key=key.strip())
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_deleted#saved", status_code=303
        )
