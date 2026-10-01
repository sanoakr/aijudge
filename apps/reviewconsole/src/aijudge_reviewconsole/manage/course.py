"""コースの設定（共通設定・基本情報・共通ルーブリック・採点設定・提出形式・自動確定）と、
コースの作成・削除・複製（段階 4-8）。

コース単位の値。**猶予や減点の段のような運用値はコースに持ち**、教員が学期中に変える
（CLAUDE.md）。科目プロファイル（採点の雛形）は読むだけで、ここで触るのはコースごとの
上書き（`aijudge_grading.overrides`）。`manage/__init__.py` の `register()` が、元のコース
設定のルートがあった位置でここの `register` を呼ぶ（散らばっていた採点設定・雛形の
ダウンロードも同じ位置に寄せた）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_audit import AuditAction
from aijudge_authoring import render_markdown
from aijudge_core import (
    DEFAULT_UPLOAD_SUFFIXES,
    DIVISIONS,
    SUFFIX_GROUPS,
    Aggregation,
    EvaluatorKind,
    Role,
    format_term,
    normalize_suffixes,
    offered_years,
)
from aijudge_core.ids import CourseId
from aijudge_course_admin import rubric
from aijudge_course_admin.bundles import MAX_ARCHIVE_BYTES
from aijudge_course_admin.course_copy import duplicate_course
from aijudge_course_admin.course_definition import course_template
from aijudge_course_admin.courses import delete_course
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.grading_settings import save as save_grading_settings
from aijudge_course_admin.grading_settings import template_of, try_settings
from aijudge_course_admin.grading_settings import validate as validate_grading_settings
from aijudge_course_admin.late_penalty import describe as describe_penalty
from aijudge_course_admin.late_penalty import parse_steps as parse_penalty_steps
from aijudge_course_admin.late_penalty import to_rows as penalty_rows
from aijudge_course_admin.operations import ensure_course
from aijudge_course_admin.roster import RosterError, parse_roster
from aijudge_course_admin.syllabus import (
    MAX_SYLLABUS_BYTES,
    SyllabusError,
    SyllabusReader,
    read_document,
    to_markdown,
)
from aijudge_eval_code_test_runner import EVALUATOR_ID as CODE_TEST_RUNNER
from aijudge_eval_code_test_runner import LANGUAGES
from aijudge_grading import LOCKED_KEYS, EvaluatorRegistry, OverrideError, effective_profile
from aijudge_grading.overrides import diff
from aijudge_identity import AuthService

from ..audit_context import recorder_for
from ..urls import RedirectResponse
from .common import (
    _console,
    _is_admin,
    _parse_minutes,
    _require_admin,
    _require_instructor,
    _role_counts,
)
from .grading_views import (
    _aggregation_from_form,
    _course_rubric_rows,
    _declared_rows,
    _default_rubric_criteria,
    _effective_profile_of,
    _evaluator_rows,
    _refuse_undeclared,
    _rubric_from_form,
    _transcription_note,
    _undeclared_evaluators,
)
from .messages import SAVED_MESSAGES


def _artifact_kind_rows() -> list[dict[str, str]]:
    """提出物の種別と、それに当たる拡張子（#316）。

    **表は 1 つだけ**（`aijudge_core.uploads.SUFFIX_KINDS`）。画面に種別の
    一覧を書き写すと、拡張子を足した日にそこだけが古くなる ── 種別は拡張子
    から決まる（`kind_for`）ので、表を畳めば選択肢になる。
    """
    from aijudge_core import SUFFIX_KINDS

    grouped: dict[str, list[str]] = {}
    for suffix, kind in SUFFIX_KINDS.items():
        grouped.setdefault(kind.value, []).append(suffix)
    return [
        {"name": kind, "suffixes": " ".join(sorted(suffixes))}
        for kind, suffixes in sorted(grouped.items())
    ]


def _collect_overrides(form) -> dict:
    """画面から来た値を上書きの形にする。**空欄は上書きしない。**

    空欄を 0 や空文字として保存すると、雛形に戻したいのか 0 にしたいのかが
    区別できなくなる。空欄は「雛形のまま」である。
    """

    def value(name: str) -> str:
        return str(form.get(name) or "").strip()

    overrides: dict = {}

    runner: dict = {}
    if value("language"):
        runner["language"] = value("language")
    if value("case_timeout_seconds"):
        runner["case_timeout_seconds"] = _positive_number(
            value("case_timeout_seconds"), "テストケースの上限"
        )
    if value("compile_timeout_seconds"):
        runner["compile_timeout_seconds"] = _positive_number(
            value("compile_timeout_seconds"), "コンパイルの上限"
        )
    judge: dict = {}
    if value("samples"):
        judge["samples"] = int(_positive_number(value("samples"), "サンプル数"))
    # 提出の遵守（#316）。**いままで YAML にしか無かった** ── 効いているのに
    # 画面のどこからも読めず、直すにはサーバ上のファイルを触るしかなかった。
    #
    # 上書きはそのコースにしか効かないので、教員が触ってよい（`overrides` の
    # 冒頭）。雛形（`subjects/*.yaml`）は読み取り専用のまま。
    compliance: dict = {}
    kinds = [name for name in form.getlist("required_kinds") if name]
    if kinds:
        compliance["required_kinds"] = kinds
    if value("filename_pattern"):
        pattern = value("filename_pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            # **保存させない。** 壊れた正規表現は採点の時に落ちる ── そのとき
            # 画面に出るのは「体裁が判定できません」だけで、原因が読めない。
            raise HTTPException(
                status_code=400, detail=f"ファイル名の規則が正規表現として不正です: {exc}"
            ) from None
        compliance["filename_pattern"] = pattern
    options = {}
    if runner:
        options["code_test_runner"] = runner
    if judge:
        options["rubric_ai_judge"] = judge
    if compliance:
        options["submission_compliance"] = compliance
    if options:
        overrides["evaluator_options"] = options

    if value("timeout_seconds"):
        overrides["timeout_seconds"] = _positive_number(value("timeout_seconds"), "評価器の上限")

    if value("blind_sample_rate"):
        rate = _positive_number(value("blind_sample_rate"), "blind 抽出率", allow_zero=True)
        if rate > 1:
            raise HTTPException(status_code=400, detail="blind 抽出率は 0〜1 で指定してください")
        overrides["measurement"] = {"blind_sample_rate": rate}

    policy: dict = {}
    for name, label in (
        ("confidence_below", "確信度の水準"),
        ("boundary_score", "合否の境界"),
        ("boundary_margin", "境界の幅"),
    ):
        if value(name):
            number = _positive_number(value(name), label, allow_zero=True)
            if number > 1:
                raise HTTPException(status_code=400, detail=f"{label}は 0〜1 で指定してください")
            policy[name] = number
    if policy:
        overrides["review_policy"] = policy

    for key, field in (("deterministic", "deterministic"), ("ai_evaluators", "ai_evaluators")):
        chosen = [name for name in form.getlist(field) if name]
        if chosen:
            overrides[key] = chosen
    return overrides


def _course_page(
    templates: Jinja2Templates,
    request: Request,
    me,
    course,
    *,
    saved: str = "",
    note: str | None = None,
    trial=None,
    values=None,
) -> Response:
    """コースの共通設定の画面。

    採点設定もここに出す。**別のページに分けない** ── 雛形からの差分は
    コースの設定の一部で、他の設定と行き来しながら決めるものだから。
    """
    console = _console(request)
    registry = EvaluatorRegistry().load_installed()
    base = template_of(course, console.profiles_dir)
    current = values if values is not None else course.grading_overrides
    try:
        applied = effective_profile(base, current, registry)
    except OverrideError:
        applied = base

    with console.database.unit_of_work() as uow:
        enrollments = uow.identity.list_enrollments(course.id)
        # 学習者の提出があるコースは消せない（#156）。**何件あるかを
        # 先に出す** ── 押してから断られるより、押す前に理由が読める方が
        # よい。教員の動作確認（trial・#108）は数えない（消せる）。
        # **数えるだけなので、行は持ってこない**（#219）。`is_trial` が
        # 列になったので SQL の側で分けられる。
        learner_submissions = uow.submissions.count_for_course(course.id).learner
    people_count = len(enrollments)

    return templates.TemplateResponse(
        request,
        "manage_course.html",
        {
            "me": me,
            "course": course,
            "section": {"label": "共通設定", "href": f"/manage/courses/{course.id}"},
            "saved": note or SAVED_MESSAGES.get(saved),
            # 試行が断られた理由。試行の結果と同じ場所に出す（`#trial-result`）。
            "trial_note": note,
            "saved_key": saved,
            # コースの削除は作成と同じくテナント管理者だけ（#156）。
            # 担当教員には出さない ── 押せないものを見せない。
            "is_admin": _is_admin(request, me),
            "learner_submissions": learner_submissions,
            # 受付のときに書き起こされるもの（#351）。**観点が読むのは
            # その本文である**ことを、評価器を割り当てる画面で言う。
            # 上書きを当てた後の `applied` で見る ── 書き起こすかどうかは
            # コースの上書きで変わりうる。
            "transcription": _transcription_note(
                applied, course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES
            ),
            "people_count": people_count,
            "role_counts": _role_counts(enrollments),
            # シラバスの本文は Markdown。素のまま出すと見出しも箇条書きも
            # 記号のまま並ぶ（課題文で実際に起きた・`statement.py`）。
            "description_html": (
                render_markdown(course.description) if course.description else None
            ),
            # 遅延の減点ルール（ADR 0013）。% で出す（保存は割合）。
            "penalty_rows": _penalty_form_rows(course),
            "suffix_groups": SUFFIX_GROUPS,
            "course_suffixes": course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES,
            # 束の上限（#161）。**画面に書く値をコードから取る** ──
            # 書き写すと、上限を変えた日に画面だけが古い数字を出す。
            "bundle_max_mb": MAX_ARCHIVE_BYTES // (1024 * 1024),
            # 複製先の学期の選択肢（#170）。作成フォームと同じ語彙から
            # 取る（#167）── 画面ごとに書き写すと、片方だけが古くなる。
            "term_years": offered_years(),
            "term_divisions": DIVISIONS,
            # 共通ルーブリック。未設定なら組み込みの既定を出して、
            # **いま何が使われているか**を見えるようにする。
            "rubric_rows": rubric.to_rows(
                rubric.from_stored(course.rubric) if course.rubric else _default_rubric_criteria()
            ),
            "rubric_is_default": not course.rubric,
            # **既定の観点が、この科目では誰にも採点できない場合。**
            #
            # 組み込みの既定は「正しさ（テスト実行）＋読みやすさ」で、
            # 正しさの担当は `code_test_runner` である。テスト実行を走らせ
            # ない科目（レポートなど）のコースがこの既定のままだと、その
            # 観点は**恒久的に未採点**になり、総点は伏せられる（ADR 0015）。
            # 設定はどこも正しく見えるのに点が出ない、という形で現れる
            # ので、画面から理由が読めない ── だからここで言う。
            #
            # **黙って別の既定に差し替えない。** 何を問うかは科目の中身で、
            # 機械が決めてよいことではない（設計原則 P5）。言うだけにする。
            "rubric_default_is_unscorable": (
                not course.rubric and CODE_TEST_RUNNER not in applied.deterministic
            ),
            # -- 採点設定 --
            "base": base,
            "profile": applied,
            "overrides": current,
            "changed": diff(base, current),
            "locked": LOCKED_KEYS,
            # インストール済みから選ばせる。**自由入力にしない** ── 存在しない
            # 名前を書けると、その科目の採点が恒久的に失敗する。
            "deterministic": _evaluator_rows(registry, EvaluatorKind.DETERMINISTIC),
            "ai_evaluators": _evaluator_rows(registry, EvaluatorKind.AI),
            # 共通ルーブリックの編集欄に出す選択肢は、**科目が宣言している
            # ものに絞る**（`_declared_rows`）。上の「使う評価器」は全部を
            # 出す ── そこで宣言を増やす。
            "rubric_deterministic": _declared_rows(
                _evaluator_rows(registry, EvaluatorKind.DETERMINISTIC), applied
            ),
            "rubric_ai_evaluators": _declared_rows(
                _evaluator_rows(registry, EvaluatorKind.AI), applied
            ),
            "undeclared": dict(_undeclared_evaluators(applied, _course_rubric_rows(course))),
            # 提出の遵守が見る値（#316）。選択肢は拡張子の表から作る。
            "artifact_kinds": _artifact_kind_rows(),
            "languages": sorted(LANGUAGES),
            "trial": trial,
        },
    )


# 減点ルールの入力欄に、いまの段のほかに足しておく空の行の数。
PENALTY_BLANK_ROWS = 2


def _penalty_form_rows(course) -> list[dict[str, str]]:
    """減点ルールの入力欄。いまの段に空の行を足して出す（段を足せるように）。"""
    return penalty_rows(course.late_penalty_steps) + [
        {"hours": "", "percent": ""} for _ in range(PENALTY_BLANK_ROWS)
    ]


def _grading_changed(course, overrides: dict, profiles_dir, registry) -> bool:
    """採点設定が**効き方として**変わったか。

    画面は雛形の値（使う評価器など）を入れた状態で出すので、開いてそのまま送ると
    雛形と同じ値が「上書き」として届く。上書きの辞書どうしで比べると、それだけで
    「変わった」ことになり、雛形からの差分でない上書きが溜まる。**効くプロファイル**
    で比べる。いまの上書きが読めない（壊れている）ときは、辞書で比べる。
    """
    current = course.grading_overrides or {}
    if overrides == current:
        return False
    try:
        before = validate_grading_settings(course, current, profiles_dir, registry)
        after = validate_grading_settings(course, overrides, profiles_dir, registry)
    except AdminError:
        return True
    return before.model_dump() != after.model_dump()


def _positive_number(raw: str, label: str, *, allow_zero: bool = False) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label}の形式が不正です: {raw!r}") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise HTTPException(status_code=400, detail=f"{label}は正の値にしてください")
    return value


def _read_body(text: str, upload: UploadFile | None, payload: bytes | None) -> str:
    """本文を決める。ファイルが選ばれていればそちらを読む。

    ファイルからの読み取りは Markdown に均して返す（`syllabus.to_markdown`）。
    そのままだと行が細かく割れていて、教員が直すにも読みにくい。
    """
    if upload is not None and upload.filename and payload:
        if len(payload) > MAX_SYLLABUS_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"ファイルが大きすぎます（上限 {MAX_SYLLABUS_BYTES // 1024} KB）",
            )
        try:
            return read_document(payload, Path(upload.filename).suffix)
        except SyllabusError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return text.strip()


def _rubric_key(criteria) -> list[dict[str, object]]:
    """ルーブリックを「画面で同じに見えるか」で比べるための形。

    **評価器の空欄は `rubric_ai_judge` のことである**（観点の選択欄の既定・
    `_criterion_fields.html`）。組み込みの既定は名前で持ち、画面から返ると空欄に
    なる ── そのまま比べると、開いて保存し直しただけで「変わった」ことになり、
    既定が明示の宣言に化ける。
    """
    rows = rubric.to_rows(criteria)
    for row in rows:
        if row.get("evaluator") in (None, "rubric_ai_judge"):
            row["evaluator"] = ""
    return rows


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.post("/courses")
    def create_course(
        request: Request,
        code: Annotated[str, Form()],
        title: Annotated[str, Form()],
        term_year: Annotated[int, Form()],
        term_division: Annotated[str, Form()],
        profile: Annotated[str, Form()],
        instructors: Annotated[str, Form()],
    ) -> Response:
        """コースを作る。**担当教員を 1 名以上、明示的に指定させる**（#130）。

        指定が無いまま作れてしまうと、誰も教えていないコースができる
        ── 「作成者を機械的に教員にする」だけでは、管理者自身が実際に
        教えるとは限らない（コースを立てるのと教えるのは別の役目）。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        # **学期は選ばせる**（#167）。年度と区分の 2 つの `<select>` から
        # 正準形式を組み立てる。組み立てるのは `aijudge_core.terms` で、
        # ここでは文字列を作らない ── 学期は course_id の素材なので、
        # 形を知っている場所を増やすと、増えた場所の分だけ表記がゆれる。
        try:
            term = format_term(term_year, term_division.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 名簿の貼り付けと同じ書式（`add_enrolments`）。1 行 1 ID でよい。
        # `parse_roster` は有効な行が 1 つも無ければ自分で例外にする
        # （「1 名以上」の要求はこれで足りる）。
        try:
            entries = parse_roster(instructors, default_role=Role.INSTRUCTOR)
        except (RosterError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logins = [entry.login for entry in entries]

        with console.database.unit_of_work() as uow:
            unknown = [
                login
                for login in logins
                if uow.identity.find_user_by_login(me.tenant_id, login) is None
            ]
        if unknown:
            # 画面からは既存利用者の指定だけを許す（`add_enrolments` と同じ
            # 規則）。新規作成はパスワード配布が伴うので CLI に回す。
            raise HTTPException(
                status_code=400,
                detail=(
                    f"利用者が未登録です: {', '.join(unknown[:10])}"
                    f"{' ほか' if len(unknown) > 10 else ''}。"
                    "新規作成は `aijudge-admin` で行ってください"
                ),
            )

        try:
            course, _created = ensure_course(
                console.database,
                tenant_id=me.tenant_id,
                code=code.strip(),
                title=title.strip(),
                term=term,
                subject_profile=profile.strip(),
                profiles_dir=console.profiles_dir,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            audit = recorder_for(uow, request, me)
            # 作った本人も担当教員にする。でないと自分のコースが見えない
            # （テナント管理者が受講登録なしで全コースに届くようになるまでの
            # 措置。#128 が入ればここは指定した教員だけで足りる）。
            auth.enroll(
                tenant_id=me.tenant_id,
                course_id=course.id,
                user_id=me.user_id,
                role=Role.INSTRUCTOR,
            )
            granted = [me.login]
            for login in logins:
                user = uow.identity.find_user_by_login(me.tenant_id, login)
                if user is not None and user.id != me.user_id:
                    auth.enroll(
                        tenant_id=me.tenant_id,
                        course_id=course.id,
                        user_id=user.id,
                        role=Role.INSTRUCTOR,
                    )
                    granted.append(login)
            # 担当教員の付与はコースの全成績に届く権限である。**1 行にまとめる**
            # ── ここは 1 回の操作なので、人ごとに散らすと後から画面での 1 操作を
            # 復元できない。
            audit.record(
                AuditAction.ENROLLED,
                target_type="course",
                target_id=str(course.id),
                summary=f"担当教員を {len(granted)} 名割り当てた",
                detail={"role": Role.INSTRUCTOR.value, "logins": granted},
            )
            uow.commit()
        return RedirectResponse(f"/courses/{course.id}", status_code=303)

    @router.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_settings(
        request: Request,
        course_id: str,
        saved: str = "",
        tasks: int = 0,
        skipped: int = 0,
    ) -> Response:
        """コースの**共通設定** ── 基本情報・受講者・自動確定・提出形式・採点設定。

        **画面の見出し・区画名・帯の項目名は「共通設定」で揃える**（#300）。
        帯が「問題セット」と呼びながらこの画面へ送っていたので、押した先が
        目当ての画面かどうかを見出しで確かめられなかった。

        課題（日程・一括確定・追加）はここには出さない。問題セットのページに
        分けてある（`unit_settings`）。1 枚に積むと、教員は「ex03 の締切を
        直す」ために縦に長い画面を目で探すことになる。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        # 複製の直後は**何件写したかを告げる**（#170）。件数だけを黙って
        # 変えると、教員は自分が何を手に入れたのか画面から確かめられない。
        note = None
        if saved == "course_duplicated":
            note = (
                f"複製しました（課題 {tasks} 件）。日程は写していません —— "
                "元の学期の日付を持ち込むと、初日から全課題が締切済みになるためです。"
                "問題セットのページで日程を入れてください。受講登録は空です。"
            )
            if skipped:
                note += f" 未承認などの理由で写さなかった課題が {skipped} 件あります。"
        return _course_page(templates, request, me, course, saved=saved, note=note)

    @router.post("/courses/{course_id}/settings")
    async def save_course_settings(request: Request, course_id: str) -> Response:
        """共通設定を**まとめて**保存する（2026-09-26）。

        以前は自動確定・提出形式・共通ルーブリック・採点設定がそれぞれ別の
        フォームで、1 つを保存すると頁が読み直され、他で書きかけていた値が
        黙って消えた。

        **全部を検査してから、1 度に書く。** どれか 1 つでも断られたら何も
        書かない。検査は個別の経路と同じ関数を通す（`rubric.parse`・
        `_refuse_undeclared`・`validate_grading_settings`）。観点が指名する
        評価器は、**同時に送られてきた採点設定で**確かめる ── 評価器を足して、
        それを使う観点を同じ保存で足す、が通るように。

        **書くのは変わった項目だけ**（監査の記録もそれだけ）。組み込みの既定の
        ルーブリックを開いて保存し直しただけで、既定が明示の宣言に化けない。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        registry = EvaluatorRegistry().load_installed()
        form = await request.form()

        minutes = _parse_minutes(str(form.get("after_minutes") or ""))
        suffixes = normalize_suffixes([str(v) for v in form.getlist("suffix")])
        if not suffixes:
            raise HTTPException(
                status_code=400, detail="提出できるファイル形式を 1 つ以上選んでください"
            )
        overrides = _collect_overrides(form)
        # **欄が送られてきたときだけ読む。** 欄の無い古い画面から保存されると、
        # 空の入力が「減点ルールを消す」に化ける。印（`penalty_present`）で見分ける。
        penalty_steps = course.late_penalty_steps
        if form.get("penalty_present"):
            try:
                penalty_steps = parse_penalty_steps(
                    list(
                        zip(
                            [str(v) for v in form.getlist("penalty_hours")],
                            [str(v) for v in form.getlist("penalty_percent")],
                            strict=False,
                        )
                    )
                )
            except AdminError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            criteria = rubric.parse(_rubric_from_form(form))
            aggregation = _aggregation_from_form(form) or Aggregation.OR
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        shown_rubric = (
            rubric.from_stored(course.rubric) if course.rubric else _default_rubric_criteria()
        )
        grading_changed = _grading_changed(course, overrides, console.profiles_dir, registry)
        rubric_changed = _rubric_key(criteria) != _rubric_key(shown_rubric) or (
            aggregation != (course.rubric_aggregation or Aggregation.OR)
        )
        # **検査するのは変えた節だけ**（個別の保存と同じ）。触っていない節に元から
        # 食い違いがあると、自動確定の分数を直すだけの保存まで断られる。
        if grading_changed or rubric_changed:
            try:
                profile = validate_grading_settings(
                    course, overrides, console.profiles_dir, registry
                )
            except AdminError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            if rubric_changed:
                _refuse_undeclared(profile, criteria, course_id=course_id)
        update: dict[str, object] = {}
        records: list[tuple[str, dict]] = []
        if minutes != course.auto_finalize_after_minutes:
            update["auto_finalize_after_minutes"] = minutes
            records.append(
                (
                    "自動確定までの猶予を変えた",
                    {
                        "auto_finalize_after_minutes": {
                            "before": course.auto_finalize_after_minutes,
                            "after": minutes,
                        }
                    },
                )
            )
        if penalty_steps != course.late_penalty_steps:
            update["late_penalty_steps"] = penalty_steps
            # **前後を % で残す。** 減点は採点のときに焼き付くので（ADR 0013）、
            # いつから何が効いたかは、この記録でしか後から追えない。
            records.append(
                (
                    "遅延の減点ルールを変えた",
                    {
                        "late_penalty_steps": {
                            "before": describe_penalty(course.late_penalty_steps),
                            "after": describe_penalty(penalty_steps),
                        }
                    },
                )
            )
        # 並びではなく集合で比べる（`normalize_suffixes` は並べ替える）。
        if set(suffixes) != set(course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES):
            update["upload_suffixes"] = suffixes
            records.append(("提出できるファイル形式を変えた", {"upload_suffixes": list(suffixes)}))
        if rubric_changed:
            update["rubric"] = tuple(c.model_dump() for c in criteria)
            update["rubric_aggregation"] = aggregation
            # ルーブリックは採点の基準そのもの。**観点の中身は書かない**
            # （長く、`detail` の上限に収まらない）── 何観点になったかと
            # 集約の仕方だけ残し、中身は課題の版が持つ（P8）。
            records.append(
                (
                    f"共通ルーブリックを保存した（{len(criteria)} 観点）",
                    {
                        "criteria": {"before": len(course.rubric), "after": len(criteria)},
                        "aggregation": {
                            "before": getattr(course.rubric_aggregation, "value", None),
                            "after": aggregation.value,
                        },
                    },
                )
            )
        if grading_changed:
            update["grading_overrides"] = overrides
            records.append(("採点設定を変えた", {"keys": sorted(overrides)}))

        if not update:
            return RedirectResponse(
                f"/manage/courses/{course_id}?saved=unchanged#saved", status_code=303
            )
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(course.model_copy(update=update))
            recorder = recorder_for(uow, request, me)
            for summary, detail in records:
                recorder.record(
                    AuditAction.COURSE_UPDATED,
                    target_type="course",
                    target_id=course_id,
                    summary=summary,
                    detail=detail,
                )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=course_settings#saved", status_code=303
        )

    @router.post("/courses/{course_id}/auto-finalize")
    def set_auto_finalize(
        request: Request,
        course_id: str,
        after_minutes: Annotated[str, Form()] = "",
    ) -> Response:
        """締切から成績を自動確定するまでの猶予（分）。**コースの既定である。**

        問題セットで指定があればそちらが勝つ（`grace_minutes`）。

        空なら自動確定しない。**既定はそれ**で、教員が明示的に入れて初めて
        自動確定が始まる。既定で自動確定させると、設定を知らない教員の
        コースで成績が勝手に閉じる。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        minutes = _parse_minutes(after_minutes)

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(update={"auto_finalize_after_minutes": minutes})
            )
            recorder_for(uow, request, me).record(
                AuditAction.COURSE_UPDATED,
                target_type="course",
                target_id=course_id,
                summary="自動確定までの猶予を変えた",
                detail={
                    "auto_finalize_after_minutes": {
                        "before": course.auto_finalize_after_minutes,
                        "after": minutes,
                    }
                },
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=course_grace#saved", status_code=303
        )

    @router.get("/courses/{course_id}/basics", response_class=HTMLResponse)
    def basics(request: Request, course_id: str, saved: str = "") -> Response:
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return templates.TemplateResponse(
            request,
            "manage_basics.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "コースの基本情報",
                    "href": f"/manage/courses/{course.id}/basics",
                },
                "title_value": course.title,
                "description_value": course.description or "",
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.post("/courses/{course_id}/basics/read", response_class=HTMLResponse)
    async def read_basics(
        request: Request,
        course_id: str,
        text: Annotated[str, Form()] = "",
        upload: UploadFile | None = None,
    ) -> Response:
        """シラバスを読んで、本文を Markdown に整えて欄に入れる。**保存はしない。**

        読み取りと登録を分ける ── 出てきたものを教員が確かめてから保存する
        （モデルが整えた文であって、シラバスそのものではない）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))

        payload = await upload.read() if upload is not None and upload.filename else None
        body = _read_body(text, upload, payload)
        if len(body) < 40:
            raise HTTPException(
                status_code=400,
                detail="PDF を選ぶか、本文を貼り付けてください（40 文字以上）",
            )
        note = "読み取りました"
        try:
            basics = SyllabusReader().read_basics(body)
            markdown = basics.markdown.strip() or body
            title = basics.title.strip()
        except Exception:
            # **モデルが使えなくても読み取りは終わらせる。** 体裁が
            # 整わないだけで、本文は取れている（規則での整形に落とす）。
            markdown, title = to_markdown(body), ""
            note = "読み取りました（本文の整形は簡易版です — S6 に繋がりませんでした）"

        return templates.TemplateResponse(
            request,
            "manage_basics.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "コースの基本情報",
                    "href": f"/manage/courses/{course.id}/basics",
                },
                "title_value": title or course.title,
                "description_value": markdown,
                # 読み取りの結果は読み取りのボタンの隣に出す。**登録の合図とは
                # 別にする** ── どちらの操作が効いたのか分からなくなる。
                "read_note": note,
                "saved": None,
            },
        )

    @router.post("/courses/{course_id}/basics/apply")
    def apply_basics(
        request: Request,
        course_id: str,
        title: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """基本情報を保存する。**コードと学期は変えない。**

        あの 2 つは（テナント・コード・学期）でコースの同一性を作っており、
        変えると別のコースになる。作り直しは新しいコースの追加で行う。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        name = title.strip()
        if not name:
            raise HTTPException(status_code=400, detail="コース名を入れてください")
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(
                    update={"title": name, "description": description.strip() or None}
                )
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/basics?saved=basics#saved", status_code=303
        )

    @router.post("/courses/{course_id}/delete")
    def delete_course_route(request: Request, course_id: str) -> Response:
        """コースを消す。**学習者の提出が無いときだけ**（#156）。

        権限はコースの作成と同じ**テナント管理者**。担当教員には開けない
        ── コースを消すのは、そのコースの中の操作ではない。

        規則は `aijudge_course_admin.courses` に置いてある（画面と CLI の両方から
        使うので、どちらが正しいかを問わずに済むよう 1 か所にする）。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        with console.database.unit_of_work() as uow:
            course = uow.identity.get_course(CourseId(course_id))
        if course is None or course.tenant_id != me.tenant_id:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        try:
            delete_course(console.database, course_id=course.id, artifact_store=console.store)
        except AdminError as exc:
            # **なぜ消せないかをその場に出す**（提出が何件あるか）。
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # 消したコースの画面はもう無いので、担当コースの一覧へ戻す。
        return RedirectResponse("/?saved=course_deleted#saved", status_code=303)

    @router.post("/courses/{course_id}/duplicate")
    def duplicate_course_route(
        request: Request,
        course_id: str,
        code: Annotated[str, Form()],
        title: Annotated[str, Form()],
        term_year: Annotated[int, Form()],
        term_division: Annotated[str, Form()],
    ) -> Response:
        """コースを複製する（#170）。

        権限はコースの作成・削除と同じ**テナント管理者** ── コースを作る
        操作である。

        規則は `aijudge_course_admin.course_copy` に置いてある（何を引き継ぎ、何を
        引き継がないかは運用の判断で、画面の都合ではない）。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        with console.database.unit_of_work() as uow:
            course = uow.identity.get_course(CourseId(course_id))
        if course is None or course.tenant_id != me.tenant_id:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        try:
            term = format_term(term_year, term_division.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            copied = duplicate_course(
                console.database,
                source_id=course.id,
                code=code.strip(),
                title=title.strip(),
                term=term,
                authored_by=me.user_id,
                profiles_dir=console.profiles_dir,
                artifact_store=console.store,
            )
        except AdminError as exc:
            # **なぜ複製できないかをその場に出す**（同じコードと学期など）。
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with console.database.unit_of_work() as uow:
            # 複製した本人を担当教員にする。受講登録は引き継がないので、
            # ここで入れないと**自分が作ったコースが自分に見えない**
            # （作成の経路と同じ理由・#130）。
            AuthService(uow.identity, audit=uow.audit).enroll(
                tenant_id=me.tenant_id,
                course_id=copied.course.id,
                user_id=me.user_id,
                role=Role.INSTRUCTOR,
            )
            recorder_for(uow, request, me).record(
                AuditAction.COURSE_UPDATED,
                target_type="course",
                target_id=str(copied.course.id),
                summary=f"コースを複製した（{course.code} → {copied.course.code}）",
                detail={"source_course_id": str(course.id), "term": term},
            )
            uow.commit()

        # 複製後にすることは日程の入力と設定の確認なので、その入口に落とす。
        return RedirectResponse(
            f"/manage/courses/{copied.course.id}"
            f"?saved=course_duplicated&tasks={copied.tasks}&skipped={len(copied.skipped)}#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/rubric")
    async def save_course_rubric(request: Request, course_id: str) -> Response:
        """コースの共通ルーブリック。**新しい課題がこれを引き継ぐ。**

        既にある課題は変わらない ── 出題済みの版は書き換えない（P8）。
        個別に直したい課題は、その課題の訂正から観点を宣言する。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        try:
            criteria = rubric.parse(_rubric_from_form(form))
            aggregation = _aggregation_from_form(form) or Aggregation.OR
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _refuse_undeclared(
            _effective_profile_of(console, course, None), criteria, course_id=course_id
        )

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(
                    update={
                        "rubric": tuple(c.model_dump() for c in criteria),
                        "rubric_aggregation": aggregation,
                    }
                )
            )
            # ルーブリックは採点の基準そのもの。**観点の中身は書かない**
            # （長く、`detail` の上限に収まらない）── 何観点になったかと
            # 集約の仕方だけ残し、中身は課題の版が持つ（P8）。
            recorder_for(uow, request, me).record(
                AuditAction.COURSE_UPDATED,
                target_type="course",
                target_id=course_id,
                summary=f"共通ルーブリックを保存した（{len(criteria)} 観点）",
                detail={
                    "criteria": {"before": len(course.rubric), "after": len(criteria)},
                    "aggregation": {
                        "before": getattr(course.rubric_aggregation, "value", None),
                        "after": aggregation.value,
                    },
                },
            )
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}?saved=rubric#saved", status_code=303)

    @router.post("/courses/{course_id}/upload-formats")
    def set_upload_formats(
        request: Request,
        course_id: str,
        suffix: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
    ) -> Response:
        """コースの既定の提出形式。**課題ごとの指定がこれを上書きする。**

        コースで一度決めておけば個々の課題では触らずに済み、レポート 1 問だけ
        PDF を許す、といった例外は課題側で足せる（`uploads.allowed_suffixes`）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        suffixes = normalize_suffixes(suffix)
        if not suffixes:
            raise HTTPException(status_code=400, detail="形式を 1 つ以上選んでください")
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(course.model_copy(update={"upload_suffixes": suffixes}))
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}?saved=formats#saved", status_code=303)

    @router.post("/courses/{course_id}/grading", response_class=HTMLResponse)
    async def save_grading(request: Request, course_id: str) -> Response:
        """保存、または試走。**試走は保存しない。**

        項目が多いので、フォーム全体を読む（宣言した引数では追いつかない）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        registry = EvaluatorRegistry().load_installed()

        form = await request.form()
        overrides = _collect_overrides(form)
        # 試行はまとめて保存のフォームから `?action=try` で来る（ボタンの値は、
        # 古いブラウザの fetch では送られないことがある ── 試行のつもりが保存に
        # ならないよう、送り先の側で言う）。
        action = str(form.get("action") or request.query_params.get("action") or "save")

        if action == "try":
            try:
                trial = try_settings(
                    console.database,
                    course,
                    overrides,
                    profiles_dir=console.profiles_dir,
                    registry=registry,
                )
            except AdminError as exc:
                return _course_page(templates, request, me, course, note=str(exc), values=overrides)
            return _course_page(templates, request, me, course, trial=trial, values=overrides)

        try:
            save_grading_settings(
                console.database,
                course,
                overrides,
                profiles_dir=console.profiles_dir,
                registry=registry,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse(f"/manage/courses/{course_id}?saved=grading#saved", status_code=303)

    @router.get("/course-template.yaml")
    def course_template_download(request: Request) -> Response:
        """コース定義のひな形（#292 と同じ流れの入口）。

        教員が埋めて管理者に渡し、管理者が `course apply` で投入する。
        **教員にも出す** ── 管理者だけに出すと、依頼する側が形式を知る手段が
        画面に無い。中身は静的で、学習者のデータは含まない。
        """
        from ..app import require_principal

        require_principal(request)
        return Response(
            course_template(),
            media_type="application/x-yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="course.yaml"'},
        )
