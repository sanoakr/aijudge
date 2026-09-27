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

import difflib
import json
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_authoring import (
    DraftKind,
    TaskChecks,
    TaskDraftRecord,
    TaskSpec,
    images,
    render_statement,
)
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_core import MIN_JUSTIFICATION_LENGTH, ReviewState, new_id
from aijudge_core.ids import CourseId
from aijudge_course_admin import rubric
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.bundle_plan import plan_bundle
from aijudge_course_admin.bundles import read_bundle, template_bundle
from aijudge_course_admin.drafting import TaskDrafter
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.kc import assert_registered
from aijudge_course_admin.task_relocation import compose_key
from aijudge_course_admin.task_verifier import TaskVerifier
from aijudge_grading import EvaluatorRegistry, load_profile

from ..overview import empty_unit, find_unit, load_units, unit_key
from ..urls import RedirectResponse
from .common import _console, _course_kcs, _normalized_unit, _require_instructor
from .course import register as register_course
from .groups import register as register_groups
from .kc import register as register_kc
from .learners import register as register_learners
from .messages import SAVED_MESSAGES
from .subjects import register as register_subjects
from .task_data import register as register_task_data
from .task_ops import register as register_task_ops
from .task_page import _kept_cases, _language_of, _save_revision, _task_of
from .tasks import register as register_tasks
from .units import register as register_units
from .users import register as register_users

# ルータは `register()` の中で毎回作る。モジュール階層に置くと、
# `create_app` を 2 回呼んだときに同じ経路が二重に登録される
# （テストで複数のアプリを作ると起きる。FastAPI が Duplicate Operation ID を
# 警告して気づいた）。


def _statement_diff(before: str, after: str) -> tuple[dict[str, str], ...]:
    """問題文の差分（#306）。行単位の unified 差分を、画面が描ける形で返す。

    **承認は差分を見て決めるもの**である。承認待ちの一覧は新しい版の本文を
    出すだけだったので、改訂を承認する人は書き換わった問題文を頭から読み直す
    ことになっていた ── どこが変わったのかは、読み比べないと分からない。

    文脈は 2 行。**全文の差分にしない** ── 長い課題文では変わっていない行が
    画面を埋め、変わった行が探しにくくなる。
    """
    rows: list[dict[str, str]] = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2):
        if line.startswith("+++") or line.startswith("---"):
            continue
        kind = (
            "meta"
            if line.startswith("@@")
            else "add"
            if line.startswith("+")
            else "del"
            if line.startswith("-")
            else "same"
        )
        rows.append({"kind": kind, "text": line})
    return tuple(rows)


def _gate_report(profile, spec, course, authored_by):
    """下書きに門をかけ、結果を返す（#321・ADR 0008）。

    **保存はしない。** 下書きは課題ではないので、検査の記録を課題版に紐づける
    場所が無い ── 結果は下書きそのものが持つ（`TaskDraftRecord.checks`）。

    仮の課題版を組んで検査に渡す。**採点と同じ経路で走らせる**ためで、
    ここだけ別の検査を書くと、門を通ったのに採点で落ちる下書きができる
    （`TaskVerifier` の冒頭と同じ判断）。

    検査そのものが動かないことはある（サンドボックス不在）。**そのときは
    None。** 「検査していない」は「合格」ではない（`VerificationReport`）。
    """
    from aijudge_authoring import build_task_version

    try:
        candidate = build_task_version(
            spec,
            course_id=course.id,
            subject_profile=course.subject_profile,
            authored_by=authored_by,
        )
        verifier = TaskVerifier(EvaluatorRegistry().load_installed(), profile)
        return TaskChecks(verification=verifier.verify(candidate), checked_at=datetime.now(UTC))
    except Exception:
        return None


def _units_of(console, course):
    """このコースの問題セット。承認画面の選択肢に要る。"""
    with console.database.unit_of_work() as uow:
        return load_units(uow, course)


def _place_in_unit(console, course, task, target: str) -> None:
    """課題を問題セットへ置く。**日程は置いた先に揃える。**

    `move_task_to_unit` が課題の移動でしていることと同じで、承認のときにも
    要る（#84）── 作問の時点ではセットを決めないので、承認が「どこに出すか」
    を決める唯一の場面になる。規則を 2 か所に書くと、片方だけ直る。
    """
    with console.database.unit_of_work() as uow:
        siblings = [
            other
            for other in uow.tasks.list_for_course(course.id)
            if other.id != task.id and unit_key(other) == quote(target, safe="")
        ]
        head = sorted(siblings, key=lambda item: item.sort_key)[0] if siblings else None
        update: dict[str, object] = {"unit": target}
        if head is not None:
            update |= {
                "session": head.session,
                "opens_at": head.opens_at,
                "submissions_open_at": head.submissions_open_at,
                "due_at": head.due_at,
                "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
            }
        positions = [other.position for other in siblings if other.position is not None]
        update["position"] = max(len(siblings), max(positions, default=0)) + 1
        uow.tasks.save_task(task.model_copy(update=update))
        uow.commit()


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


def generate_task(
    request: Request,
    course_id: str,
    unit: str,
    key_suffix: Annotated[str, Form()],
    kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
    difficulty: Annotated[str, Form()] = "standard",
    instructions: Annotated[str, Form()] = "",
    test_cases: Annotated[str, Form()] = "5",
    readability_weight: Annotated[str, Form()] = "0.3",
) -> Response:
    """AI に課題を 1 つ作らせる。**承認するまで出題されない**（P5）。

    **入口は作問ページ 1 つ**（`POST /drafts/generate`・#522）。以前は問題
    セットの画面にも生成フォームがあり、入口が 2 つに分かれていた（#84 で作問の
    区分を作ったあとも残っていた）。`unit` は出題先の**候補**で、承認のときに
    変えられる。

    **KC は登録済みからの選択だけ。** モデルはもっともらしいキーを
    いくらでも作るので、自由入力にすると体系が静かに荒れる
    （`aijudge_course_admin.kc` の規則 4）。

    `avoid_similar_to` にはこのコースの既存課題を入れる ── 「似せない」
    材料が無いと、既存課題の言い換えが出てくる。

    生成物はここでは保存するだけで、門・解答可能性・重複の検査は
    `aijudge-authoring` が担う（ADR 0008）。ここが返すのは候補であって
    課題ではない。
    """
    from ..app import require_principal

    me = require_principal(request)
    course = _require_instructor(request, me, CourseId(course_id))
    console = _console(request)

    chosen = tuple(k.strip() for k in kc if k.strip())
    if not chosen:
        raise HTTPException(status_code=400, detail="知識要素を 1 つ以上選んでください")
    try:
        # 選択肢は登録済みから出しているが、直接叩かれる経路もある。
        assert_registered(console.database, chosen, course_keys=course.knowledge_components)
    except AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    key = _normalized_unit(unit)
    profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
    with console.database.unit_of_work() as uow:
        siblings = [
            task for task in uow.tasks.list_for_course(CourseId(course_id)) if unit_key(task) == key
        ]
        # **似せないための材料。** 既存課題の本文を渡す（学習者のデータは
        # 含まないので、外部モデルにも渡してよい・設計原則 P7）。
        avoid = []
        for task in uow.tasks.list_for_course(CourseId(course_id)):
            version = uow.tasks.latest_version(task.id)
            if version is not None:
                avoid.append(version.statement)

    head = siblings[0] if siblings else None
    full_key = compose_key(head.unit if head else unit, key_suffix.strip())
    if not full_key:
        raise HTTPException(status_code=400, detail="課題キーを入力してください")

    try:
        blueprint = Blueprint(
            knowledge_components=chosen,
            subject_profile=course.subject_profile,
            # **コースの範囲を渡す。** KC は「何を問うか」を決めるが、
            # 「どこまでを既習として書いてよいか」は決めない。空なら
            # 節ごと出さない（`aijudge_course_admin.drafting._course_section`）。
            course_title=course.title,
            course_outline=course.description or "",
            difficulty=Difficulty(difficulty),
            language=_language_of(profile),
            instructions=tuple(line.strip() for line in instructions.splitlines() if line.strip()),
            avoid_similar_to=tuple(avoid[:20]),
            test_case_count=int(test_cases or 5),
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"生成の指定が不正です: {exc}") from None

    try:
        result = TaskDrafter().draft(blueprint, key=full_key)
    except Exception as exc:  # 生成の失敗は運用の事象。画面に理由を返す。
        raise HTTPException(
            status_code=502,
            detail=f"課題を生成できませんでした（S6 が止まっている可能性があります）: {exc}",
        ) from exc

    spec = result.spec.model_copy(update={"readability_weight": float(readability_weight or 0.0)})
    # **課題にはしない。下書きとして置く**（#321）。課題にすると、そこで
    # 同一性（課題キー → 課題 ID）が決まってしまう ── 生成物は提案であって
    # 確定ではないので、名前を含めて承認のときに決められる必要がある（P5）。
    draft = TaskDraftRecord(
        id=new_id("dft"),
        course_id=course.id,
        kind=DraftKind.NEW,
        spec=spec,
        unit=unit.strip() or (head.unit if head is not None else ""),
        generated_by=result.model,
        generation_prompt_version=result.prompt_id,
        created_by=me.user_id,
        created_at=datetime.now(UTC),
        # **門を通して記録する**（ADR 0008・#267）。判断材料が無いまま
        # 承認を求めない。走らせられない環境では None のままになる。
        checks=_gate_report(profile, spec, course, me.user_id),
        subject_profile=course.subject_profile,
        readability_weight=float(readability_weight or 0.0),
    )
    with console.database.unit_of_work() as uow:
        uow.tasks.save_draft(draft)
        uow.commit()

    # **承認する場所へ送る。** 課題はまだ無いので、問題セットへ戻しても
    # そこには何も増えていない（増えるのは承認したとき・#84）。
    return RedirectResponse(
        f"/manage/courses/{course_id}/drafts?saved=generated#saved", status_code=303
    )


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

    @router.get("/courses/{course_id}/drafts", response_class=HTMLResponse)
    def draft_queue(request: Request, course_id: str, saved: str = "", unit: str = "") -> Response:
        """レビュー待ちの生成課題。

        **科目プロファイルと違い、ここはブラウザから触ってよい**（ADR 0002）。
        あちらは評価器の指名とタイムアウトを持つ採点の設定で、壊すと全員の
        採点が止まる。課題を承認するかどうかは、まさに教員が決めることである。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        rows = []
        with console.database.unit_of_work() as uow:
            for draft in uow.tasks.list_drafts(course.id):
                # 改訂なら、いま学習者に出ている版と比べる（#306）。
                published = (
                    uow.tasks.latest_published_version(draft.task_id)
                    if draft.task_id is not None
                    else None
                )
                rows.append(
                    {
                        "draft": draft,
                        "revision": draft.kind is DraftKind.REVISION,
                        "published": published,
                        # 改訂の差分。**承認は差分を見て決めるもの**で、
                        # 書き換わった問題文を頭から読み直させない。
                        "diff": (
                            _statement_diff(published.statement, draft.spec.statement)
                            if published is not None
                            else ()
                        ),
                        # **提出済みであることを言う。** 出題済みの課題の問題文を
                        # 書き換えると、既に提出した学習者と後から提出する学習者で
                        # 違う問題になる。過去の採点は自分の版を指したまま残る
                        # （P8）ので壊れないが、承認するかどうかの判断材料である。
                        "submissions": (
                            uow.tasks.submission_count(draft.task_id)
                            if draft.task_id is not None
                            else 0
                        ),
                        "kc_keys": draft.spec.knowledge_components,
                        # 検査していない下書きも並べる。**隠さない** ── 見えない
                        # ものは承認も却下もされず、待ち行列に溜まり続ける。
                        "checks": draft.checks,
                        "clean": bool(draft.checks and draft.checks.verification.usable),
                        # 学習者に出る形（#105 と同じ関数）。承認は「学生が読む
                        # 画面」を見て決めるもので、Markdown の生文だけでは
                        # 数式・コードの囲み・画像が意図どおりかが分からない。
                        "statement_html": render_statement(draft.spec.statement),
                    }
                )
        return templates.TemplateResponse(
            request,
            "manage_drafts.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "未承認の課題（AI 作問）",
                    "href": f"/manage/courses/{course.id}/drafts",
                },
                "rows": rows,
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # 承認のときに出題先を選ぶ（#84）。**作問の時点では決めない。**
                "units": [
                    {"key": group.key, "unit": group.unit, "label": group.label}
                    for group in _units_of(console, course)
                    if group.unit
                ],
                # AI 作問の入口はここだけ（#522）。問題セットの画面から来たら、
                # そのセットを出題先の候補に選んでおく（`?unit=`）。
                "kcs": _course_kcs(console, course),
                "candidate_unit": unit,
                "difficulties": [d.value for d in Difficulty],
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
            },
        )

    @router.post("/courses/{course_id}/drafts/generate")
    def generate_without_unit(
        request: Request,
        course_id: str,
        key_suffix: Annotated[str, Form()],
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        difficulty: Annotated[str, Form()] = "standard",
        instructions: Annotated[str, Form()] = "",
        test_cases: Annotated[str, Form()] = "5",
        readability_weight: Annotated[str, Form()] = "0.3",
        unit: Annotated[str, Form()] = "",
    ) -> Response:
        """AI に課題を作らせる。**AI 作問の入口はここだけ**（#522）。

        **出題先は承認のときに決める**（#84）。`unit` は候補で、空なら決めない。
        使えるかどうかは作ってみないと分からないので、先に決めて却下すると、
        そのセットの一覧に残骸が並ぶ。問題セットの画面からは、そのセットを候補に
        選んだ状態でここへ来る（`?unit=`）。
        """
        return generate_task(
            request,
            course_id,
            unit,
            key_suffix=key_suffix,
            kc=kc,
            difficulty=difficulty,
            instructions=instructions,
            test_cases=test_cases,
            readability_weight=readability_weight,
        )

    @router.post("/courses/{course_id}/drafts/{draft_id}")
    async def decide_draft(request: Request, course_id: str, draft_id: str) -> Response:
        """下書きを**採用**するか**捨てる**（#321）。

        採用したときに初めて課題になる ── それまで課題は存在しない
        （`aijudge_authoring.draft_store` の冒頭）。だから**採用の瞬間まで
        何でも直せる**: 課題キー、題名、問題文、出題する問題セット。

        **捨てるのは即時の削除である**（ADR 0019）。却下した下書きは残さない
        ── 承認率を測るための記録も残らないが、その数は「生成の質」ではなく
        「いまのプロンプト × この教員の好み」を測っており、単一の合格基準に
        する意味が無いと判断した（2026-09-15）。

        改訂の下書き（#306）は**キーを変えられない** ── 同じ課題の書き直しで
        あって別の課題ではない。採用すると新しい版になる。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        decision = str(form.get("decision") or "")
        with console.database.unit_of_work() as uow:
            draft = uow.tasks.get_draft(draft_id)
        if draft is None or draft.course_id != course.id:
            # 存在と権限を区別しない（他コースの下書きを探らせない）。
            raise HTTPException(status_code=404, detail="下書きが見つかりません")

        if decision != "approve":
            # **捨てる。** 理由は聞かない（記録しないのだから聞く意味が無い）。
            with console.database.unit_of_work() as uow:
                uow.tasks.delete_draft(draft_id)
                uow.commit()
            return RedirectResponse(
                f"/manage/courses/{course_id}/drafts?saved=draft_dropped#saved", status_code=303
            )

        statement = str(form.get("statement") or draft.spec.statement)
        title = str(form.get("title") or draft.spec.title or "").strip() or None
        unit = str(form.get("unit") or draft.unit).strip()
        if draft.kind is DraftKind.REVISION:
            # 改訂はキーを変えない（同じ課題の書き直し）。
            key = draft.spec.key
        else:
            suffix = str(form.get("key_suffix") or "").strip()
            key = compose_key(unit, suffix) or draft.spec.key
        if not key:
            raise HTTPException(status_code=400, detail="課題キーを入力してください")

        try:
            spec = draft.spec.model_copy(
                update={
                    "key": key,
                    "title": title,
                    "statement": statement,
                    "unit": unit or draft.spec.unit,
                }
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"課題の指定が不正です: {exc}") from None

        if draft.kind is DraftKind.REVISION:
            # **既にある課題の新しい版にする。** `save_task` は第 1 版を作る
            # 経路なので、同じ鍵で呼ぶと「内容が違う」と断られる（P8）。
            task = _task_of(console, course, str(draft.task_id))
            with console.database.unit_of_work() as uow:
                current = uow.tasks.latest_version(draft.task_id)
            if current is None:
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            saved = _save_revision(
                console,
                me,
                course,
                task,
                current,
                statement=statement,
                criteria=rubric.from_criteria(current.criteria),
                aggregation=current.aggregation,
                position=task.position,
                accepted=task.accepted_suffixes,
                reference_solution=current.reference_solution,
                test_cases=_kept_cases(current, editing=()),
                knowledge_components=draft.spec.knowledge_components or None,
                generated_by=draft.generated_by or None,
                generation_prompt_version=draft.generation_prompt_version or None,
                # **採用した時点で承認済み。** 承認の段はここ 1 つで足りる。
                review_state=ReviewState.APPROVED,
            )
        else:
            try:
                saved = save_task(
                    console.database,
                    course_id=course.id,
                    spec=spec,
                    subject_profile=draft.subject_profile or course.subject_profile,
                    authored_by=me.user_id,
                    # **出所は残す**（P8）。承認したのは人だが、書いたのは
                    # モデルである ── どのプロンプト版が出したものかを辿れる。
                    generated_by=draft.generated_by or None,
                    generation_prompt_version=draft.generation_prompt_version or None,
                    review_state=ReviewState.APPROVED,
                )
            except AdminError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 検査の記録は課題版に紐づけ直す（下書きは消える）。
        if draft.checks is not None:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_checks(saved.version.id, draft.checks)
                uow.commit()

        # 日程と提出形式は問題セットから引き継ぐ（手で足した課題と同じ）。
        if unit:
            _place_in_unit(console, course, saved.task, unit)

        with console.database.unit_of_work() as uow:
            uow.tasks.delete_draft(draft_id)
            uow.commit()

        console.last_task = (str(course.id), saved)
        return RedirectResponse(
            f"/manage/courses/{course_id}/drafts?saved=draft_approved#saved", status_code=303
        )

    return router
