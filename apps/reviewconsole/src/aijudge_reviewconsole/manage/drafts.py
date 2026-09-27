"""下書き・AI 作問（段階 4-13）。

**AI の生成物は提案であって課題ではない**（P5・#321）。生成は下書きとして置き、採用した
瞬間に初めて課題になる（キー・題名・問題文・出題先はそれまで直せる）。捨てるのは即時の
削除（ADR 0019）。AI 作問の入口は作問ページ 1 つ（#522）。`manage/__init__.py` の
`register()` が、元のルートがあった位置でここの `register` を呼ぶ。
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_authoring import DraftKind, TaskChecks, TaskDraftRecord, render_statement
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_core import MIN_JUSTIFICATION_LENGTH, ReviewState, new_id
from aijudge_core.ids import CourseId
from aijudge_course_admin import rubric
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.drafting import TaskDrafter
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.kc import assert_registered
from aijudge_course_admin.task_relocation import compose_key
from aijudge_course_admin.task_verifier import TaskVerifier
from aijudge_grading import EvaluatorRegistry, load_profile

from .. import notices
from ..overview import load_units, unit_key
from ..urls import RedirectResponse
from .common import _console, _course_kcs, _normalized_unit, _require_instructor
from .messages import SAVED_MESSAGES
from .task_page import _kept_cases, _language_of, _save_revision, _task_of


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


def _units_of(console, course):
    """このコースの問題セット。承認画面の選択肢に要る。"""
    with console.database.unit_of_work() as uow:
        return load_units(uow, course)


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


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

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

        console.notices.put(me.user_id, course.id, notices.TASK_SAVED, saved)
        return RedirectResponse(
            f"/manage/courses/{course_id}/drafts?saved=draft_approved#saved", status_code=303
        )
