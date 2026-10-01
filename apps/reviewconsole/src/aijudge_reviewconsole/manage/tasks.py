"""課題の本文の編集（段階 4-10）。

新規・編集・訂正・AI 改訂・版の復元・知識要素の候補・課題文のプレビュー。

教員が問題文を書き、直す流れ。保存は `task_page._save_revision`（訂正）と
`aijudge_course_admin.authoring.save_task`（新規）を通り、API と同じ経路になる。
`manage/__init__.py` の `register()` が、元の課題のルートがあった位置でここの `register` を呼ぶ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_authoring import (
    DraftKind,
    TaskDraftRecord,
    TaskSpec,
    render_markdown,
    render_statement,
)
from aijudge_authoring.spec import TestCaseSpec
from aijudge_core import DEFAULT_UPLOAD_SUFFIXES, may_see, new_id
from aijudge_core.ids import CourseId, TaskId, TaskVersionId
from aijudge_course_admin import rubric
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.kc import allowed_namespaces, assert_registered, list_for_namespaces
from aijudge_course_admin.revision import TaskReviser
from aijudge_course_admin.syllabus import TaskKcReader
from aijudge_course_admin.task_relocation import compose_key
from aijudge_course_admin.test_cases import TestCaseWriter
from aijudge_eval_code_test_runner import EVALUATOR_ID as CODE_TEST_RUNNER
from aijudge_grading import EvaluatorRegistry, load_profile

from .. import notices
from ..companion_view import companion_view
from ..overview import unit_key
from ..urls import RedirectResponse
from .common import (
    _can_edit,
    _console,
    _course_kcs,
    _kc_keys_of,
    _normalized_unit,
    _require_instructor,
    _require_reader,
)
from .grading_views import (
    _aggregation_from_form,
    _effective_profile_of,
    _refuse_undeclared,
    _rubric_from_form,
)
from .task_page import (
    KEEP,
    _cases_by_shape,
    _chosen_suffixes,
    _data_driven_criteria,
    _kept_cases,
    _key_of,
    _language_of,
    _record_gates,
    _save_revision,
    _task_of,
    _task_page,
)


def _reference_answer_from(form, *, default):
    """フォームの参照回答例（AI にだけ渡す・学生には見えない）。

    **欄が無ければ `default`**（訂正ではいまの版から引き継ぐ `KEEP`）。欄があって
    空なら「無し」── 消したつもりの回答例が残らないようにする。
    """
    if "reference_answer" not in form:
        return default
    text = str(form.get("reference_answer") or "").replace("\r\n", "\n").strip()
    return text or None


def _kc_candidates_for(
    console, course, statement: str, *, current: tuple[str, ...], reference_solution
) -> dict:
    """問題文と参照解答から、その課題の知識要素の**追加と削除の候補**を出す（#496）。

    **選ばせるのはコースが使っている知識要素だけ**（#318）。以前は科目の
    名前空間にある語彙すべてから選ばせ、「このコースでは未使用のもの」も
    候補に並べていた ── 課題に付けるとコースの範囲にも入るので、**課題を
    直すつもりの操作でコースの設定が変わる**。コースに何を置くかは
    `/manage/courses/{id}/kc` で決めることで、課題の編集の副作用にしない。

    以前はシラバス用の読み手（`SyllabusReader.propose`）を流用していた。返り値が
    空の `{}` を許し、名前の無いキーだけを渡し、参照解答もいま付いているものも
    渡していなかったので、当てはまる課題でも 0 件になった（prog2 ex2）。
    課題 1 問向けの読み手（`TaskKcReader`）にし、削除の候補も出す。
    """
    profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
    namespaces = allowed_namespaces(profile)
    vocabulary = list_for_namespaces(console.database, namespaces, include_deprecated=False)
    known = {kc.key for kc in vocabulary}
    # **このコースが使うものだけを見せる。** 語彙の全体を渡すと、その中から
    # 選ばれてしまう。名前も渡す ── キーだけでは日本語の課題文と突き合わない。
    in_course = {
        kc.key: kc.label for kc in vocabulary if kc.key in set(course.knowledge_components)
    }
    try:
        result = TaskKcReader().select(
            statement,
            vocabulary=in_course,
            current=current,
            reference_solution=reference_solution,
        )
    except Exception as exc:  # 生成の失敗は運用の事象。理由を画面に返す。
        raise HTTPException(status_code=502, detail=f"候補を作れませんでした: {exc}") from exc
    suggested = [
        {
            "key": use.key,
            "label": in_course.get(use.key, use.key),
            "evidence": use.evidence,
            "attached": use.key in current,
        }
        for use in result.add
    ]
    remove = [
        {"key": gone.key, "label": in_course.get(gone.key, gone.key), "reason": gone.reason}
        for gone in result.remove
    ]
    # コースの外（語彙には登録済み）から出てきた候補は、コースに足す導線を出す。
    # **黙って落とさない** ── 件数と理由を出し、足したいならコースの知識要素で足す。
    outside = [d.key for d in result.discarded if d.key.strip() in known]
    discarded = [d for d in result.discarded if d.key.strip() not in known]
    return {
        "suggested": suggested,
        "remove": remove,
        "outside": outside,
        "discarded": discarded,
        "empty": not result.add and not result.remove,
    }


def _kcs_from_form(console, course, form) -> tuple[str, ...]:
    """フォームの知識要素（#292）。**コースが使うものからしか選べない**（#322）。

    以前は、課題に付けた知識要素をコースの範囲にも足していた（付ける＝
    このコースが使う）。それは**コースの設計を課題の側から膨らませる**
    ことになる ── コースが何を教えるかは知識要素の画面で決めることで、
    1 問ずつの編集の副産物にしてよいものではない。範囲の外のキーが来たら
    断る（画面は範囲内しか出さないので、来るのは API か古い画面である）。

    キーは登録済みの語彙のものだけ（2026-09-13 決定: 画面から語彙は
    増やさない）。
    """
    chosen = tuple(dict.fromkeys(str(v).strip() for v in form.getlist("kc") if str(v).strip()))
    try:
        assert_registered(
            console.database,
            chosen,
            # **範囲の検査も同じ関門に通す**（`aijudge_course_admin.kc`）── 画面で
            # 絞るだけにすると、API 経由の投入が素通りする。
            course_keys=course.knowledge_components,
        )
    except AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return chosen


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.post("/courses/{course_id}/statement-preview", response_class=HTMLResponse)
    async def statement_preview(
        request: Request,
        course_id: str,
        statement: Annotated[str, Form()] = "",
    ) -> Response:
        """書きかけの問題文を、**学習者に出るのと同じ描画**で返す（#105）。

        ブラウザ側で Markdown を描かない。課題文の描画は `html=False`・数式は
        サーバ側 MathML・画像の幅は属性プラグイン（`statement.py`）で、
        JavaScript の Markdown 実装は**普通の文章では一致し、間違いが起きる
        ところ（数式・画像・生 HTML の遮断）でだけ食い違う**。それではプレビュー
        ではなく別の意見になる。

        返すのは本文の断片だけ。保存はしない ── 版が上がるのは「保存」で行う。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        return HTMLResponse(render_statement(statement))

    @router.post("/courses/{course_id}/tasks")
    async def add_task(
        request: Request,
        course_id: str,
        statement: Annotated[str, Form()],
        key: Annotated[str, Form()] = "",
        key_suffix: Annotated[str, Form()] = "",
        unit: Annotated[str, Form()] = "",
        position: Annotated[str, Form()] = "",
        readability_weight: Annotated[str, Form()] = "0.3",
        suffix: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        formats: Annotated[str, Form()] = "",
        no_auto_tests: Annotated[str, Form()] = "",
        test_case_count: Annotated[str, Form()] = "5",
    ) -> Response:
        """課題を 1 件足す。

        **保存の中身は API と同じ経路を通る**（`aijudge_course_admin.authoring.save_task`）。
        経路ごとに組み立て方が分かれると、「画面から作った課題だけ観点が
        1 つ足りない」が起きる。実際に起きた ── 廃止した zip 取り込みは
        `readability_weight` が 0.0 固定で、画面から入れた課題には AI 観点が
        付かなかった。

        テストケースはここでは**貼らせない**。1 件ずつ入力させる画面にすると、
        実在する規模（1 課題 7 件 × 48 課題）で現実的でない。まとまった
        投入は API を使う。

        **科目が `code_test_runner` を宣言していれば、代わりに生成する。**
        テストケースの無い課題は正しさの観点が AI 判定に落ちる
        （`TaskSpec.auto_graded`）ので、テスト実行で確定できるはずの科目でも
        全課題が教員の確定待ちになる。生成物は**参照解答と一緒に作らせて門を
        通し、承認待ちで保存する**（`aijudge_course_admin.test_cases`）。

        「自動テストを使わない」を選べば従来どおり ── C の科目にも設計を問う
        記述課題はあり、そこに自動テストを強いる理由が無い。

        **日程は指定させない。** 問題セットの値をそのまま引き継ぐ ── 課題
        ごとに違う締切を持てると、同じセットの中で締切がずれる。変えたい
        ときはセットの日程を変える（`set_unit_schedule`）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        # **キーの前半は問題セットが決める。** 回のページから追加する限り
        # `ex02/p8` の `ex02/` は動かず、教員が打つのは `p8` だけである。
        # 打たせると `ex2/p8` のような取り違えが混ざり、鍵は同一性そのもの
        # なので、取り違えたぶんは別の課題として増える。
        full_key = key.strip() or compose_key(unit.strip(), key_suffix.strip())
        if not full_key:
            raise HTTPException(status_code=400, detail="課題キーを入力してください")

        # 日程と回番号は問題セットから引き継ぐ。空のセット（最初の 1 問）は
        # まだ日程を持たないので、そのまま空で入る。
        with console.database.unit_of_work() as uow:
            siblings = [
                task
                for task in uow.tasks.list_for_course(course.id)
                if unit_key(task) == quote(unit.strip() or "_", safe="")
            ]
        head = siblings[0] if siblings else None

        # **テストで確定できる科目なら、テストケースを用意する。**
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        wants_tests = CODE_TEST_RUNNER in profile.deterministic and not no_auto_tests.strip()
        generated = None
        generation_failed = False
        if wants_tests:
            try:
                generated = TestCaseWriter().write(
                    statement,
                    language=_language_of(profile),
                    count=int(test_case_count or 5),
                )
            except Exception:
                # **課題を作れなくしない**（設計原則 P2）。S6 が止まっている
                # あいだ作問が止まると、教員は授業の準備そのものができない。
                # テストケースの無い課題として保存し、**そうなったことを言う**
                # ── 黙って落とすと、テスト実行で確定する課題を作ったつもりの
                # まま学期を過ごすことになる。
                generation_failed = True

        # 知識要素（#292）。付けるものはコースの範囲にも入れる。
        form = await request.form()
        components = _kcs_from_form(console, course, form)

        try:
            spec = TaskSpec(
                key=full_key,
                statement=statement,
                unit=unit.strip() or None,
                position=int(position) if position.strip() else None,
                readability_weight=float(readability_weight or 0.0),
                knowledge_components=components,
                reference_solution=None if generated is None else generated.reference_solution,
                reference_answer=_reference_answer_from(form, default=None),
                test_cases=(
                    ()
                    if generated is None
                    else tuple(
                        TestCaseSpec(name=case.name, input=case.input, expected=case.expected)
                        for case in generated.test_cases
                    )
                ),
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"課題の指定が不正です: {exc}") from None

        try:
            saved = save_task(
                console.database,
                course_id=course.id,
                spec=spec,
                subject_profile=course.subject_profile,
                authored_by=me.user_id,
                course_rubric=course.rubric,
                # **生成したなら承認待ちにする**（P5）。門は「参照解答とテストが
                # 整合している」までしか言わず、問題文の意図と合っているかは
                # 見ていない。人が一度見るまで出題しない。
                generated_by=None if generated is None else generated.model,
                generation_prompt_version=None if generated is None else generated.prompt_id,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 門 1・門 2 を通し、結果を残す（生成課題と同じ・ADR 0008）。
        if generated is not None:
            _record_gates(console, profile, saved.version)

        # 日程と回番号は問題セットから引き継ぐ。`TaskSpec` を通さないのは、
        # あれが API の語彙でもあり、日程を課題単位で受け取る口を増やすと
        # 「セットで揃える」という規則が守られない経路ができるため。
        # 提出形式は課題ごとの性質。日程と違い、最初の 1 問でも指定できる。
        accepted = _chosen_suffixes(suffix, formats, course)
        if head is None:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_task(saved.task.model_copy(update={"accepted_suffixes": accepted}))
                uow.commit()
        else:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_task(
                    saved.task.model_copy(
                        update={
                            "session": head.session,
                            "opens_at": head.opens_at,
                            "submissions_open_at": head.submissions_open_at,
                            "due_at": head.due_at,
                            "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
                            "accepted_suffixes": accepted,
                        }
                    )
                )
                uow.commit()

        console.notices.put(me.user_id, course.id, notices.TASK_SAVED, saved)
        # テストで確定できる科目なのにテストケースが無いなら、そう言う。
        if saved.version.test_cases or CODE_TEST_RUNNER not in profile.deterministic:
            landed = "task"
        elif generation_failed:
            landed = "task_generation_failed"
        else:
            landed = "task_without_tests"
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{saved.task.id}/edit?saved={landed}#saved",
            status_code=303,
        )

    @router.get("/courses/{course_id}/tasks/{task_id}/edit", response_class=HTMLResponse)
    def edit_task(
        request: Request, course_id: str, task_id: str, saved: str = "", why: str = ""
    ) -> Response:
        """既にある課題を直す画面。**問題セットのページには展開しない。**

        ルーブリックと問題文は横幅いっぱいで読むものなので、一覧の中に
        畳んで置くと段階の説明が読めない。
        """
        from ..app import require_principal

        me = require_principal(request)
        course, role = _require_reader(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        # **公開前の試験は TA に見せない**（`may_see`）。読むだけの画面でも、
        # 問題文とテストケースが出る。
        if not may_see(task, role, now=datetime.now(UTC)):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        # **TA は読むだけ**（#102）。課題文は描いて出す ── Markdown のまま
        # 出すと、採点しながら読む相手にとっては学習者に見えている画面と
        # 別物になる（`statement.py`）。
        if not _can_edit(role):
            readonly_registry = EvaluatorRegistry().load_installed()
            data_criteria = _data_driven_criteria(readonly_registry, version)
            cases_by_shape = _cases_by_shape(readonly_registry, version)
            return templates.TemplateResponse(
                request,
                "task_readonly.html",
                {
                    "me": me,
                    "course": course,
                    "section": {
                        "label": task.title,
                        "href": f"/manage/courses/{course.id}/units/{unit_key(task)}",
                    },
                    "task": task,
                    "version": version,
                    "unit_key": unit_key(task),
                    "statement_html": render_markdown(version.statement),
                    "rubric_rows": rubric.to_rows(version.criteria),
                    "accepted": task.accepted_suffixes
                    or course.upload_suffixes
                    or DEFAULT_UPLOAD_SUFFIXES,
                    # 検証データで採点する観点（#300・#302）。**TA には確認
                    # だけ。** 0 件のときに何が起きているかは教員と同じ言葉で
                    # 出す ── 「AI が判定します」と出していたので、実際には
                    # 誰も判定していないことが伝わらなかった。
                    "data_criteria": data_criteria,
                    "criterion_data": {
                        name: shape for shape, names in data_criteria.items() for name in names
                    },
                    "io_cases": cases_by_shape.get("io", ()),
                    "item_cases": cases_by_shape.get("items", ()),
                    # クライアント・サーバのケース。教員の画面と同じものを確認だけ。
                    "companion": companion_view(cases_by_shape.get("companion", ())),
                },
            )
        return _task_page(
            templates,
            request,
            me,
            course,
            unit_key_value=unit_key(task),
            task=task,
            version=version,
            saved=saved,
            # **理由をそのまま出す**（決めつけない・#52）。以前は錨に載せて
            # いたので URL の欄にしか出ず、画面には「書き直せませんでした」
            # としか出なかった ── 錨は知らせの着地点に要る。
            why=why,
        )

    @router.get("/courses/{course_id}/units/{unit}/tasks/new", response_class=HTMLResponse)
    def new_task(request: Request, course_id: str, unit: str) -> Response:
        """課題を追加する画面。訂正と同じ形。"""
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _task_page(templates, request, me, course, unit_key_value=_normalized_unit(unit))

    @router.post("/courses/{course_id}/tasks/{task_id}/kc-candidates", response_class=HTMLResponse)
    async def task_kc_candidates(request: Request, course_id: str, task_id: str) -> Response:
        """編集画面の「AI に候補を出させる」。**書きかけの問題文で出す** ──
        保存してからでないと出せないと、問題文を書く手が止まる。フォームを
        丸ごと受け取り、問題文と選択中の知識要素はそのまま画面に戻す。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        form = await request.form()
        statement = str(form.get("statement") or "") or version.statement
        if len(statement.strip()) < 20:
            raise HTTPException(status_code=400, detail="問題文が短すぎます（20 文字以上）")
        chosen = tuple(str(v) for v in form.getlist("kc") if str(v).strip())
        return _task_page(
            templates,
            request,
            me,
            course,
            unit_key_value=task.unit or "_",
            task=task,
            version=version,
            statement=statement,
            chosen_kcs=chosen,
            kc_candidates=_kc_candidates_for(
                console,
                course,
                statement,
                # **いま画面で選んでいるもの**（保存済みではなく）。削除の候補は
                # これに対して出す ── 教員が付け外しを試している途中でもよい。
                current=chosen,
                reference_solution=version.reference_solution,
            ),
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/restore")
    def restore_task_version(
        request: Request,
        course_id: str,
        task_id: str,
        version_id: Annotated[str, Form()],
    ) -> Response:
        """古い版の中身で**新しい版を作る**（#319）。

        **版は書き換えない**（P8）。戻すのは「あの中身をもう一度出す」ことで
        あって、履歴を巻き戻すことではない ── 過去の採点はそれぞれ自分の版を
        指したまま残り、どの版で付いた点かが辿れる。

        **却下した版からも戻せる。** 却下は「その版を出さない」という判断で、
        中身を二度と使えないという意味ではない ── 直して出し直すつもりの
        却下もある。写すのは教員が明示的に押したときだけである。

        できる版は**承認済み**。教員が版を選んで押した操作なので、そこに
        承認の段をもう 1 つ置く理由が無い（生成物とはそこが違う・P5）。

        **版の id は経路ではなくフォームで受け取る。** 3 つの id（コース・
        課題・版）を経路に並べると 140 字になり、運用ログが識別子として
        受け付ける長さ（128 字）を超える（`aijudge_telemetry.context`）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        with console.database.unit_of_work() as uow:
            source = uow.tasks.get_version(TaskVersionId(version_id))
            current = uow.tasks.latest_version(TaskId(task_id))
            kcs = _kc_keys_of(uow, source) if source is not None else ()
        if source is None or current is None or source.task_id != TaskId(task_id):
            raise HTTPException(status_code=404, detail="課題版が見つかりません")

        _save_revision(
            console,
            me,
            course,
            task,
            current,
            statement=source.statement,
            criteria=rubric.from_criteria(source.criteria),
            aggregation=source.aggregation,
            position=task.position,
            accepted=task.accepted_suffixes,
            reference_solution=source.reference_solution,
            test_cases=_kept_cases(source, editing=()),
            knowledge_components=kcs,
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=restored_version#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/revise-with-ai")
    def revise_task_with_ai(
        request: Request,
        course_id: str,
        task_id: str,
        instructions: Annotated[str, Form()] = "",
    ) -> Response:
        """課題をいまの基準に合わせて書き直させ、**承認待ちの版**として積む（#306）。

        **必ず承認待ちである。** 教員はまだ 1 文字も読んでいない ── 生成物は
        提案であって確定ではない（P5・ADR 0008）。承認するまで学習者には
        いまの版が出続ける。

        書き直すのは**問題文と知識要素だけ**。観点はコースの共通ルーブリックが
        持っており（`_course_rubric_rows`）、段階の記述まで決まっている ──
        そこをモデルに書かせると、コースごとに決めた段階が課題ごとに割れる。
        入出力セットと参照解答も触らない（#305 の仕事で、期待出力は走らせて
        作る）── 混ぜると、どちらの都合で期待出力が変わったのかが辿れない。

        **直すところが無ければ積まない。** 差分の無い版を承認待ちに置くと、
        教員は中身の無い版を 1 件ずつ開いて確かめることになる。

        `instructions` は教員からの指示（1 行 1 件・任意）。**何を直してほしい
        かは、読んだ教員がいちばんよく知っている** ── 観点との食い違いは機械的に
        見付かるが、「毎年ここで質問が来る」は教員しか知らない。作問の指示と
        同じ扱いで、必須事項の列ではない（`aijudge_course_admin.revision` の冒頭）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        with console.database.unit_of_work() as uow:
            version = uow.tasks.latest_version(TaskId(task_id))
            current_kcs = _kc_keys_of(uow, version) if version is not None else ()
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        rows = rubric.to_rows(version.criteria)
        try:
            revised = TaskReviser().revise(
                version.statement,
                criteria=tuple((row["title"], row["description"]) for row in rows),
                vocabulary=tuple((kc.key, kc.label) for kc in _course_kcs(console, course)),
                current_kcs=tuple(current_kcs),
                instructions=tuple(
                    line.strip() for line in instructions.splitlines() if line.strip()
                ),
            )
        except Exception as exc:
            # **理由をそのまま出す**（決めつけない・#52）。
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{task_id}/edit"
                f"?saved=revision_failed&why={quote(str(exc)[:200], safe='')}#saved",
                status_code=303,
            )

        if revised.unchanged:
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=revision_none#saved",
                status_code=303,
            )

        # **版にはしない。下書きとして置く**（#321）。承認するまで課題版を
        # 増やさない ── 却下しただけで「最新版が却下」になり、一覧が
        # 「出題されません」と嘘をつく形（#319）が根から消える。
        draft = TaskDraftRecord(
            id=new_id("dft"),
            course_id=course.id,
            kind=DraftKind.REVISION,
            task_id=task.id,
            spec=TaskSpec(
                key=_key_of(task, version),
                # **配点を引き継ぐ**（`TaskVersion.points_declared`）。渡さないと既定の
                # 100 で版が作られ、教員が入れた配点が消える。
                **({"max_score": version.max_score} if version.points_declared else {}),
                title=task.title,
                statement=revised.statement,
                unit=task.unit,
                session=task.session,
                position=task.position,
                # **観点はそのまま。** 変えるのは問題文と知識要素だけ。
                criteria=rubric.from_criteria(version.criteria),
                aggregation=version.aggregation,
                reference_solution=version.reference_solution,
                test_cases=_kept_cases(version, editing=()),
                knowledge_components=tuple(revised.knowledge_components or current_kcs),
                accepted_suffixes=task.accepted_suffixes,
            ),
            unit=task.unit or "",
            changes=revised.changes,
            generated_by=revised.model,
            generation_prompt_version=revised.prompt_id,
            created_by=me.user_id,
            created_at=datetime.now(UTC),
            subject_profile=version.subject_profile,
            accepted_suffixes=task.accepted_suffixes,
        )
        with console.database.unit_of_work() as uow:
            uow.tasks.save_draft(draft)
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/drafts?saved=revision_queued#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/revise")
    async def revise_task(request: Request, course_id: str, task_id: str) -> Response:
        """既にある課題を直す。**出題済みの版は書き換えず、版を上げる**（P8）。

        過去の採点がどの基準で付いたのかを辿れなくなるので、上書きはしない。
        内容が同じなら版は上がらない（提出形式だけ変えたい場合がこれ）。

        観点が行数ぶん並ぶので、フォーム全体を読む。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        statement = str(form.get("statement") or "")
        position = str(form.get("position") or "")
        suffix = [str(v) for v in form.getlist("suffix")]
        formats = str(form.get("formats") or "")

        try:
            criteria = rubric.parse(_rubric_from_form(form))
            aggregation = _aggregation_from_form(form)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        # **画面で観点を編集したときだけ**、科目が宣言していない評価器を断る。
        # 引き継ぐだけの経路（入出力や項目表の編集、問題文の訂正）は、既に
        # 付いている食い違いを理由に止めない ── 直す前に検証データを触れなく
        # なる。食い違いは編集画面が選択中のまま警告する（`undeclared`）。
        if criteria:
            _refuse_undeclared(
                _effective_profile_of(console, course, version), criteria, course_id=course_id
            )
        # 観点を送ってこない経路（問題文だけ直す等）では、**いまの観点を
        # そのまま引き継ぐ**。空で作り直すと、読みやすさの観点が黙って消えて
        # 次の版から採点されなくなる。
        criteria = criteria or rubric.from_criteria(version.criteria)

        # 知識要素（#292）。欄が送られてきたときだけ置き換える ── 欄を持たない
        # 経路（API 等）では None のまま渡し、いまの版のものを引き継ぐ。
        components = _kcs_from_form(console, course, form) if "kc_form" in form else None

        _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=statement,
            criteria=criteria,
            aggregation=aggregation,
            position=int(position) if position.strip() else task.position,
            # **欄が無ければ、いまの値を引き継ぐ**（2026-09-27）。既存の課題の提出形式は
            # 「この問題の設定」に移したので、内容のフォームは形式を送らない。コースの
            # 既定に落とすと、問題文を直すたびに教員が選んだ形式が消える。
            accepted=(
                _chosen_suffixes(suffix, formats, course)
                if formats or suffix
                else task.accepted_suffixes
            ),
            knowledge_components=components,
            # **テストと参照解答も引き継ぐ**（#262）。観点と同じ理由で、
            # 結果はもっと悪い ── 観点が消えれば採点されない観点が出るだけ
            # だが、テストが消えると決定的評価が何も採点できず、総合点が
            # 永久に保留になる。しかも画面には何も出ない。
            #
            # この経路に来るのは「問題文の誤字を直す」のような操作で、
            # テストを捨てる意図は無い。捨てたいときは、テストを作り直す
            # 経路（`/test-cases`）がある。
            reference_solution=version.reference_solution,
            # 参照回答例は**欄が送られてきたときだけ**書き換える（空欄なら無しにする）。
            # 欄の無いフォームから来た訂正では、いまの版の値を引き継ぐ。
            reference_answer=_reference_answer_from(form, default=KEEP),
            # **評価器と payload ごと持ち越す**（`_kept_cases`・#302）。以前は
            # 入出力の 2 欄だけを写していたので、入出力以外の検証データ
            # （項目表・パターン表・伴走プロセス）は**課題の既定の評価器あての
            # 空の入出力に書き換わった** ── 例外は出ず、次の提出が「照合する
            # 項目が無い」として落ちる。実際に prog2 ex01-2 で、問題文を保存
            # しただけで `text_pattern_check` の 3 項目が `code_test_runner`
            # の空ケースになった（2026-09-22）。
            test_cases=_kept_cases(version, editing=()),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task#saved", status_code=303
        )
