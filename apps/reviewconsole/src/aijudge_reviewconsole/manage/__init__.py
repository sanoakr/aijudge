"""コース・課題・受講の管理（教員向け）。

**YAML の直接編集を運用の前提にしない**（設計方針 §9.2 Phase 2）。学期の頭に
やることは CLI（`aijudge-admin`）でもできるが、学期中に発生する作業
── 締切の設定、受講者の追加、課題の追加 ── を教員がターミナルで行うのは
現実的でない。

課題の追加は**画面と API の両方から**でき、保存は同じ経路を通る
（`aijudge_course_admin.authoring.save_task`）。zip での一括取り込みは廃止した ── 移行元
（Sharif Judge）の形式をサーバの入口の語彙にしてしまっており、移行が
終わったあとも一生ついて回る形だった。まとまった投入は API で行う
（`api.py`）。

**コースが使っている科目プロファイル（`*.yaml`）は読み取り専用。** 1 つの
プロファイルは複数のコースの雛形になりうるので、書き換えると自分が担当して
いないコースの採点まで変わる（ADR 0002 の「コードと同じ扱いでレビューを
通す」はこの範囲のこと）。

そこで #146 で編集できる範囲を絞った ── **未参照のものだけ直接編集・改名
でき、参照中のものへの唯一の操作は「複製して編集」**（`/manage/subjects`）。
判定は `aijudge_course_admin.profiles` が持ち、この層は画面と繋ぐだけ。判定を画面に
写すと、食い違ったときに「画面では編集できるのに保存が拒否される」形で出る。
コース設定の画面（`_course_page`）から雛形を書く口は、以前どおり無い
── そこで触るのはコースごとの上書き（`aijudge_grading.overrides`）。

権限は 2 段。
- コースの作成・削除は **ADMIN**
- そのコースの課題・受講の管理は **INSTRUCTOR 以上**（TA には開けない。
  締切や受講の変更は成績に直接効く）

成績の確定もここに置く。教員の待ち行列は異議申立だけなので（ADR 0009）、
依頼が出なかった提出を閉じる導線がどこかに要る。課題ごとの一括確定と、
締切からの猶予（`auto_finalize_after_hours`）の設定がそれである
（ADR 0010）。**猶予は科目プロファイルではなくコースに持つ** ── 締切と
同じ性質の運用値で、教員が学期中に決めるものだから。

**このパッケージは画面の領域ごとに分けていく途中**（段階的な立て直しの段階 4、
地図は `docs/design/manage-split-map.md`）。移した領域のモジュールは
`register(router, templates)` を持ち、ここの `register()` がそれを**元のルートが
あった位置で**呼ぶ。FastAPI は登録順にパスを照合するので（`/tasks/new` と
`/tasks/{task_id}` など）、呼ぶ位置を変えると別のハンドラが応答しうる ── 横取りは
`test_manage_route_order.py` が見張る（ルートの写しは行を並べ替えて比べるので、
順序の変化は捕まえない）。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_authoring import TaskSpec, images
from aijudge_core.ids import CourseId
from aijudge_course_admin import rubric
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.bundle_plan import plan_bundle
from aijudge_course_admin.bundles import read_bundle, template_bundle
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.task_relocation import compose_key
from aijudge_grading import load_profile

from ..overview import empty_unit, find_unit, load_units
from ..urls import RedirectResponse
from .common import _console, _course_kcs, _normalized_unit, _require_instructor
from .course import register as register_course
from .drafts import register as register_drafts
from .groups import register as register_groups
from .kc import register as register_kc
from .learners import register as register_learners
from .subjects import register as register_subjects
from .task_data import register as register_task_data
from .task_ops import register as register_task_ops
from .tasks import register as register_tasks
from .units import register as register_units
from .users import register as register_users

# ルータは `register()` の中で毎回作る。モジュール階層に置くと、
# `create_app` を 2 回呼んだときに同じ経路が二重に登録される
# （テストで複数のアプリを作ると起きる。FastAPI が Duplicate Operation ID を
# 警告して気づいた）。


def _unit_group_or_empty(console, course, unit: str):
    """URL の鍵から問題セットを引く。**無ければ「まだ空の回」として返す。**

    回は課題が持つ属性で、それ自体の記録は無い（`create_unit` は何も保存
    しない）。だから「まだ 1 問も無い回」は存在しない回と区別できず、
    404 にすると回を作る導線が消える（`unit_settings` と同じ判断）。
    """
    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        units = load_units(uow, course)
    return find_unit(units, key) or empty_unit(key, course)


def register(templates: Jinja2Templates) -> APIRouter:
    """テンプレートを束ねてルータを返す。呼ぶたびに新しいルータを作る。"""
    router = APIRouter(prefix="/manage")

    @router.get("", response_class=HTMLResponse)
    def index() -> Response:
        """コースの一覧は担当コース（`/`）に 1 つだけ置く。

        以前はここにも一覧があり、採点の入口（`/`）と管理の入口（`/manage`）で
        同じコースが 2 度並んでいた。**入口が 2 つあること自体が構成の
        分かりにくさの元**だったので、古い経路は残したまま集約する。
        """
        return RedirectResponse("/", status_code=303)

    register_users(router, templates)

    register_subjects(router, templates)

    register_course(router, templates)

    register_units(router, templates)

    # -- 束（zip）で課題を入れる（#161）------------------------------------
    #
    # **読み取り → 確認 → 人が保存**（シラバス読み取りと同じ作法）。押した
    # 瞬間に何十件も入る操作にしない ── まとまった投入で怖いのは「押したら
    # 何件変わったか分からない」ことである。
    #
    # 受け取る構造はこのシステム自身の語彙（`aijudge_course_admin.bundles`）。
    # 移行元の形式をここに持ち込まない ── 一度その形で入口を作り、廃止した。

    @router.get("/courses/{course_id}/units/{unit}/bundle/template")
    def bundle_template(request: Request, course_id: str, unit: str) -> Response:
        """アップロードする一式のひな形を返す（#171）。

        **画面の説明文から構造を組み立てさせない。** 書き写しの失敗は
        「取り込めません」の 1 行になって返り、何を直せばよいかは画面から
        読めない。書ける状態のものを渡せば、そもそも書き写しが要らない。

        **コースに合わせて作る** ── 使える評価器・共通ルーブリックの観点
        コード・このコースが使う知識要素のキーを、`task.yaml` のコメントに
        入れる。知識要素は登録済みのものしか名指しできないので、一覧が
        手元にあるかどうかで書きやすさが変わる。

        権限は取り込みと同じ（担当教員以上・#102）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")

        payload = template_bundle(
            unit=unit,
            evaluators=tuple(profile.deterministic),
            criterion_codes=tuple(
                criterion.code for criterion in rubric.from_stored(course.rubric)
            ),
            kc_keys=tuple(kc.key for kc in _course_kcs(console, course)),
        )
        # **ファイル名は ASCII に留める。** 回の名前は教員が付けるもので、
        # 日本語も入りうる ── `filename*=` の符号化まで持ち込むより、
        # 中身の README で「どの問題セット向けか」を言う方が壊れない。
        stem = unit if unit.isascii() and unit.isprintable() else "bundle"
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{stem}-template.zip"'},
        )

    @router.post("/courses/{course_id}/units/{unit}/bundle", response_class=HTMLResponse)
    async def read_task_bundle(request: Request, course_id: str, unit: str) -> Response:
        """zip を読んで、**何が起きるかを見せる**。まだ保存しない。"""
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        # **まだ課題が 1 問も無い回にも入れられる。** 束で入れるのは
        # 「新しい問題セットを作る」でもあるので、存在しない回として断ると
        # 導線が無くなる（`unit_settings` と同じ扱い・`empty_unit`）。
        group = _unit_group_or_empty(console, course, unit)

        form = await request.form()
        upload = form.get("archive")
        payload = await upload.read() if hasattr(upload, "read") else b""
        try:
            bundled = read_bundle(payload)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        # 画像は**この時点で置く**。鍵は中身のハッシュなので、置き直しても
        # 増えず、保存しなかった場合に残るのは孤児のファイルだけ（教員が
        # 画像だけ上げて貼らなかった場合と同じ状態）。こうしておくと、
        # 確認画面はサーバに一時状態を持たずに済む。
        prepared = []
        for task in bundled:
            spec = task.spec
            statement = spec.statement
            for image in task.images:
                try:
                    name = images.new_name(image.payload, image.name)
                    key = images.storage_key(str(course.id), name)
                except images.ImageError as exc:
                    raise HTTPException(
                        status_code=400, detail=f"{task.leaf}/{image.name}: {exc}"
                    ) from None
                if not console.store.exists(key):
                    console.store.put(key, image.payload)
                # 束の中の相対リンクを、置いた先のリンクに書き換える。
                statement = statement.replace(
                    f"images/{image.name}", images.url_for(str(course.id), name)
                )
            # 鍵の前半は**この画面が持つ問題セット**が決める（#70）。
            prepared.append(
                spec.model_copy(
                    update={
                        "statement": statement,
                        "key": compose_key(group.unit or "", spec.key),
                        "unit": group.unit,
                        "session": group.session,
                    }
                )
            )

        try:
            planned = plan_bundle(
                console.database,
                course_id=course.id,
                subject_profile=course.subject_profile,
                authored_by=me.user_id,
                specs=tuple(prepared),
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        return templates.TemplateResponse(
            request,
            "manage_bundle_preview.html",
            {
                "me": me,
                "course": course,
                "unit": group,
                "planned": planned,
                # 確認画面から保存へ渡す。**サーバに一時状態を持たない** ──
                # 持つと、2 人が同時に上げたときにどちらの束か分からなくなる。
                "payload": json.dumps([item.spec.model_dump(mode="json") for item in planned]),
            },
        )

    @router.post("/courses/{course_id}/units/{unit}/bundle/confirm")
    def save_task_bundle(
        request: Request,
        course_id: str,
        unit: str,
        specs: Annotated[str, Form()],
    ) -> Response:
        """確認した束を保存する。**保存は既存の経路（`save_task`）を通す。**

        **承認済みで入る**（#522）。束は教員が問題セットのひな形を埋めて
        アップロードするもので、入れる本人が書き、確認画面で読んでいる。
        以前は既定を未承認にしていたが（#161「他所で書かれたもの」）、同じ中身を
        `course apply` やディレクトリの取り込みで入れると承認済みになる不整合と、
        未承認の版を画面から承認する経路が無い穴があった。**承認が要るのは
        AI の生成物だけ**（設計原則 P5）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group_or_empty(console, course, unit)

        try:
            # **送られてきたものを信じない。** 確認画面を経由しても、POST は
            # 手で作れる（`TaskSpec` の検証をもう一度通す）。
            parsed = tuple(TaskSpec.model_validate(item) for item in json.loads(specs))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"読み取れませんでした: {exc}") from None

        for spec in parsed:
            try:
                save_task(
                    console.database,
                    course_id=course.id,
                    spec=spec,
                    subject_profile=course.subject_profile,
                    authored_by=me.user_id,
                    revise=True,
                    course_rubric=course.rubric,
                )
            except AdminError as exc:
                raise HTTPException(status_code=409, detail=f"{spec.key}: {exc}") from None

        return RedirectResponse(
            f"/manage/courses/{course.id}/units/{group.key}?saved=bundle_saved#saved",
            status_code=303,
        )

    register_groups(router, templates)

    register_task_ops(router, templates)

    register_tasks(router, templates)

    register_task_data(router, templates)

    register_learners(router, templates)

    # ------------------------------------------------------------------
    # 知識要素（KC）の体系（設計原則 P6）
    # ------------------------------------------------------------------

    register_kc(router, templates)

    # ------------------------------------------------------------------
    # 生成された課題のレビュー（S2、設計方針 §5）
    # ------------------------------------------------------------------

    register_drafts(router, templates)

    return router
