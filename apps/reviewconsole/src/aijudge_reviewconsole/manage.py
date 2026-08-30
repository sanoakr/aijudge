"""コース・課題・受講の管理（教員向け）。

**YAML の直接編集を運用の前提にしない**（設計方針 §9.2 Phase 2）。学期の頭に
やることは CLI（`aijudge-admin`）でもできるが、学期中に発生する作業
── 締切の設定、受講者の追加、課題の追加 ── を教員がターミナルで行うのは
現実的でない。

課題の追加は**画面と API の両方から**でき、保存は同じ経路を通る
（`aijudge_admin.save_task`）。zip での一括取り込みは廃止した ── 移行元
（Sharif Judge）の形式をサーバの入口の語彙にしてしまっており、移行が
終わったあとも一生ついて回る形だった。まとまった投入は API で行う
（`api.py`）。

**科目プロファイル（`subjects/*.yaml`）は編集させない。** 表示だけする。
あれは評価器の指名とタイムアウトを持つ採点の設定であり、ブラウザから壊せる
ようにすると、1 人の操作で全員の採点が止まる。コードと同じ扱いでレビューを
通す（ADR 0002）。運用者が「いま何が設定されているか」を見られれば十分で、
それがこの画面の役割。

権限は 2 段。
- コースの作成・削除は **ADMIN**
- そのコースの課題・受講の管理は **INSTRUCTOR 以上**（TA には開けない。
  締切や受講の変更は成績に直接効く）

成績の確定もここに置く。教員の待ち行列は異議申立だけなので（ADR 0009）、
依頼が出なかった提出を閉じる導線がどこかに要る。課題ごとの一括確定と、
締切からの猶予（`auto_finalize_after_hours`）の設定がそれである
（ADR 0010）。**猶予は科目プロファイルではなくコースに持つ** ── 締切と
同じ性質の運用値で、教員が学期中に決めるものだから。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from aijudge_admin import (
    AdminError,
    allowed_namespaces,
    assert_registered,
    enrol_roster,
    ensure_course,
    finalize_task,
    finalize_tasks,
    kc_usage,
    list_for_namespaces,
    parse_roster,
    pending_counts,
    register_kc,
    restore_kc,
    retire_kc,
    rubric,
    save_grading_settings,
    save_task,
    template_of,
    try_settings,
)
from aijudge_admin.drafting import TaskDrafter
from aijudge_admin.roster import RosterError
from aijudge_admin.syllabus import (
    MAX_SYLLABUS_BYTES,
    SYLLABUS_EXAMPLE,
    SyllabusError,
    SyllabusReader,
    read_document,
    to_markdown,
)
from aijudge_authoring import TaskSpec, render_markdown
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_core import (
    DEFAULT_UPLOAD_SUFFIXES,
    MIN_JUSTIFICATION_LENGTH,
    SUFFIX_GROUPS,
    Course,
    EvaluatorKind,
    GradeWindow,
    Role,
    Task,
    normalize_suffixes,
)
from aijudge_core.ids import CourseId, TaskId, TaskVersionId, UserId, derived_id
from aijudge_eval_code_test_runner import LANGUAGES
from aijudge_grading import (
    LOCKED_KEYS,
    EvaluatorRegistry,
    OverrideError,
    effective_profile,
    load_profile,
)
from aijudge_grading.overrides import diff
from aijudge_identity import AuthService, PermissionDenied, Principal

from .overview import empty_unit, find_unit, load_units, unit_key

# ルータは `register()` の中で毎回作る。モジュール階層に置くと、
# `create_app` を 2 回呼んだときに同じ経路が二重に登録される
# （テストで複数のアプリを作ると起きる。FastAPI が Duplicate Operation ID を
# 警告して気づいた）。


def _console(request: Request):
    return request.app.state.aijudge


def _require_admin(request: Request, me: Principal) -> None:
    """テナント内に ADMIN の受講が 1 つでもあるか。

    コースの作成はコース単位の権限では表せない（まだコースが無い）。
    テナント単位の役割を持つまでの暫定で、**どこかのコースで ADMIN** を
    その代わりにしている。Phase 8 でテナント単位の役割に置き換える。
    """
    console = _console(request)
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        for course in auth.courses_for(me.tenant_id, me.user_id):
            enrollment = uow.identity.find_enrollment(course.id, me.user_id)
            if enrollment is not None and enrollment.role is Role.ADMIN:
                return
    raise HTTPException(status_code=403, detail="コースの作成には管理者権限が必要です")


def _is_admin(request: Request, me: Principal) -> bool:
    """テナント内に ADMIN の受講が 1 つでもあるか（`_require_admin` の判定版）。"""
    try:
        _require_admin(request, me)
    except HTTPException:
        return False
    return True


def _require_instructor(request: Request, me: Principal, course_id: CourseId) -> Course:
    """そのコースの教員であること。**TA には開けない。**

    締切と受講の変更は成績に直接効く。採点を分担する TA と、履修の管理を
    する教員は別の権限である。
    """
    console = _console(request)
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


@dataclass(frozen=True)
class _Merged:
    """複数課題の確定結果を 1 つに畳んだもの。画面が読む形は 1 件と同じ。"""

    task: object
    finalized: int
    contested: int


def _merged(outcomes) -> _Merged:
    return _Merged(
        task=outcomes[0].task if outcomes else None,
        finalized=sum(outcome.finalized for outcome in outcomes),
        contested=sum(outcome.contested for outcome in outcomes),
    )


def _language_of(profile) -> str:
    """この科目の言語。`code_test_runner` の設定から取る。

    プロファイルが言語を持っているのに生成側で別に指定させると、
    「C の科目に Python の課題が生成される」が起きる。
    """
    options = profile.evaluator_options.get("code_test_runner", {})
    return str(options.get("language") or "c")


def _role_counts(enrollments) -> list[dict[str, object]]:
    """役割ごとの人数。**0 名の役割も並べる。**

    総数だけでは、TA を登録し忘れているのか 0 名が正しいのかが読み取れない。
    並びは `Role` の宣言順にする（多い順にすると、コースを開くたびに順番が
    変わって目で追えない）。
    """
    counted = Counter(str(enrollment.role.value) for enrollment in enrollments)
    return [{"role": role.value, "count": counted.get(role.value, 0)} for role in Role]


def _normalized_unit(raw: str) -> str:
    """URL から来た問題セットの鍵を、`unit_key` と同じ形に揃える。

    経路パラメータは復号された状態で届くので、`quote` した鍵と直接
    比べると、記号を含む鍵（`ex 03` など）が一致しない。往復させて
    どちらの形で来ても同じ鍵になるようにする。
    """
    return quote(unquote(raw), safe="")


def _key_of(task, version) -> str:
    """この課題を作った `TaskSpec.key`。

    ID は鍵から導いてある（`derived_id`）ので、次の版の ID も観点の ID も
    鍵が無いと作れない。新しい版は鍵を持っている（`TaskVersion.source_key`）。

    **持っていない古い版のために復元を試す。** 鍵は取り込み元の
    `<まとまり>/p<番号>` の形をしており、課題 ID と突き合わせれば当たりを
    確かめられる ── 当たらなければ諦めて断る（間違った鍵で保存すると、
    別の課題を上書きする）。
    """
    if version.source_key:
        return str(version.source_key)
    for candidate in _key_candidates(task):
        if derived_id("tsk", candidate) == str(task.id):
            return candidate
    raise HTTPException(
        status_code=409,
        detail=(
            "この課題は画面から直せません（取り込み時の課題キーが記録されて "
            "いない古い課題です）。API から同じキーで入れ直してください。"
        ),
    )


def _key_candidates(task) -> list[str]:
    unit = task.unit or ""
    position = task.position
    names: list[str] = []
    if unit and position:
        names += [f"{unit}/p{position}", f"{unit}/{position}", f"{unit}/p{position:02d}"]
    if unit:
        names += [unit, f"{unit}/p1"]
    names.append(task.title)
    return names


# 保存の合図。**押したことが分かるようにする。** 同じ画面に戻る操作は、
# 成功しても見た目が変わらないので、押せていないのか効いていないのかを
# 教員が区別できない。
# 保存後の戻り先。**その場に戻す。** 画面の先頭に飛ぶと、教員は自分が
# どこを触っていたのかを探し直すことになる（設定が縦に並ぶ画面ほど効く）。
# 素の HTML でこれをやるには、リダイレクト先に錨を付けるのが確実で、
# JavaScript も要らない。
SAVED_MESSAGES: dict[str, str] = {
    "schedule": "日程を保存しました（この問題セットの全課題に反映）",
    "grace": "保存しました",
    "number": "保存しました",
    "course_grace": "保存しました",
    "formats": "保存しました",
    "enrolled": "受講登録を保存しました",
    "removed": "受講を取り消しました",
    "kc_added": "知識要素を追加しました",
    "kc_retired": "知識要素を引退させました",
    "kc_restored": "引退を取り消しました",
    "basics": "基本情報を保存しました",
    "role": "役割を変えました",
    "grading": "採点設定を保存しました",
    "order": "並びを変えました",
    "task": "課題を保存しました",
    "rubric": "共通ルーブリックを保存しました（新しい課題から使われます）",
    "generated": "課題を生成しました。承認するまで出題されません",
}


def _update_unit(
    request: Request, course_id: str, unit: str, *, update: dict, saved: str
) -> Response:
    """問題セット内の全課題に同じ更新を当てる。

    **`model_copy` を使わない。** あれは検証を走らせないので、締切が公開より
    前の課題がそのまま保存され、次に読むときに初めて落ちる（実際にそうなった）。
    作り直して検証を通す。
    """
    from .app import require_principal

    me = require_principal(request)
    _require_instructor(request, me, CourseId(course_id))
    console = _console(request)

    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        tasks = [
            task for task in uow.tasks.list_for_course(CourseId(course_id)) if unit_key(task) == key
        ]
        if not tasks:
            raise HTTPException(status_code=404, detail="この問題セットには課題がありません")
        for task in tasks:
            try:
                updated = Task.model_validate(task.model_dump() | update)
            except ValidationError as exc:
                # 日程の前後関係は模型が見ている（`Task._check_schedule`）。
                raise HTTPException(status_code=400, detail=_first_error(exc)) from None
            uow.tasks.save_task(updated)
        uow.commit()
    return RedirectResponse(
        f"/manage/courses/{course_id}/units/{key}?saved={saved}#{saved}", status_code=303
    )


def _chosen_suffixes(raw: list[str], marker: str, course: Course) -> tuple[str, ...]:
    """課題に入れる提出形式を決める。**空で保存しない。**

    画面はコースの既定をチェック済みで出すので、送られてくるのは常に
    「教員が選んだ結果」である。**1 つも選ばずに保存させない** ── 空で
    保存できると、その課題には何も提出できなくなり、原因が学習者側の
    問題に見える。

    `marker` はフォームから来たことの印。画面を経由しない呼び出し
    （API・移行）は形式を送らないので、そこではコースの既定を入れる。
    """
    chosen = normalize_suffixes(raw)
    if chosen:
        return chosen
    if marker:
        raise HTTPException(
            status_code=400, detail="提出できるファイル形式を 1 つ以上選んでください"
        )
    return course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES


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
    options = {}
    if runner:
        options["code_test_runner"] = runner
    if judge:
        options["rubric_ai_judge"] = judge
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


def _default_rubric_criteria():
    """組み込みの既定（正しさ＋読みやすさ）を宣言の形で返す。

    未設定のコースでも**いま何が使われているか**を画面に出すため。空欄を
    見せると、観点が無いのか既定なのかが分からない。
    """
    from aijudge_authoring.importers.sharif_judge import (
        correctness_criterion,
        readability_criterion,
    )

    correctness = correctness_criterion()
    return (
        correctness.model_copy(update={"weight": 0.7}),
        readability_criterion(0.3),
    )


def _rubric_from_form(form) -> list[dict[str, str]]:
    """ルーブリックの表を行に戻す。**行数は画面が決める**（増減できる）。

    `code`・`title`… の同名フィールドが行数ぶん並ぶので、位置で組み直す。
    """
    codes = form.getlist("criterion_code")
    rows: list[dict[str, str]] = []
    for index in range(len(codes)):

        def at(field: str, index: int = index) -> str:
            values = form.getlist(field)
            return str(values[index]) if index < len(values) else ""

        rows.append(
            {
                "code": at("criterion_code"),
                "title": at("criterion_title"),
                "description": at("criterion_description"),
                "weight": at("criterion_weight"),
                "evaluator": at("criterion_evaluator"),
                "levels": at("criterion_levels"),
            }
        )
    return rows


def _evaluator_rows(registry, kind) -> list[dict[str, str]]:
    """評価器の名前と 1 行説明。

    **説明は評価器が持つ**（クラスの docstring の 1 行目）。画面が名前ごとの
    表を持つと、評価器を足したときに説明だけ抜ける。
    """
    rows = []
    for name in sorted(registry.ids_of_kind(kind)):
        doc = (registry.get(name).__doc__ or "").strip()
        rows.append({"name": name, "about": doc.splitlines()[0] if doc else ""})
    return rows


def _positive_number(raw: str, label: str, *, allow_zero: bool = False) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label}の形式が不正です: {raw!r}") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise HTTPException(status_code=400, detail=f"{label}は正の値にしてください")
    return value


def _parse_minutes(raw: str) -> int | None:
    """自動確定までの猶予（分）。空なら未指定。

    0 を許すと締切と同時に確定し、締切直前の提出が採点前に確定しうる。
    猶予は正の値でなければ意味がない。
    """
    text = raw.strip()
    if not text:
        return None
    try:
        minutes = int(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"分の形式が不正です: {raw!r}") from None
    if minutes <= 0:
        raise HTTPException(status_code=400, detail="猶予は 1 分以上にしてください")
    return minutes


def _first_error(exc: ValidationError) -> str:
    """模型の検証エラーを 1 行にする。教員に読める文だけを出す。"""
    for error in exc.errors():
        message = str(error.get("msg", ""))
        return message.removeprefix("Value error, ")
    return "指定が不正です"


def _compose_key(unit: str, suffix: str) -> str:
    """問題セットの鍵と、その中での鍵を繋ぐ。

    `ex02` + `p8` → `ex02/p8`。まとまりが無い課題（未分類）は後半だけを
    鍵にする。後半に `/` が入っていればそれを尊重する ── 取り込み済みの
    課題と鍵を揃えたい場合があり、そこで縛ると直す手段が無くなる。
    """
    if not suffix:
        return ""
    if not unit or suffix.startswith(f"{unit}/"):
        return suffix
    return f"{unit}/{suffix}"


def _unit_href(course_id: str, task) -> str:
    """その課題が属する回のページ。

    課題を触る操作（締切・一括確定・追加）は**その回のページから来る**ので、
    そこへ戻す。コースのトップに返すと、教員は毎回同じ回を開き直すことになる。
    """
    return f"/manage/courses/{course_id}/units/{unit_key(task)}"


def _parse_when(raw: str) -> datetime | None:
    """`YYYY-MM-DDTHH:MM` を読む。空なら None（締切なし）。

    タイムゾーンは UTC として扱う。素の値を入れると締切判定がサーバの
    ローカル時刻に依存する（ADR 0006 で同じ罠を踏んでいる）。
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"日時の形式が不正です: {raw!r}") from exc


def register(templates) -> APIRouter:
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

    @router.post("/courses")
    def create_course(
        request: Request,
        code: Annotated[str, Form()],
        title: Annotated[str, Form()],
        term: Annotated[str, Form()],
        profile: Annotated[str, Form()],
    ) -> Response:
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)
        try:
            course, _created = ensure_course(
                console.database,
                tenant_id=me.tenant_id,
                code=code.strip(),
                title=title.strip(),
                term=term.strip(),
                subject_profile=profile.strip(),
                profiles_dir=console.profiles_dir,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 作った本人を担当教員にする。でないと自分のコースが見えない。
        with console.database.unit_of_work() as uow:
            AuthService(uow.identity).enroll(
                tenant_id=me.tenant_id,
                course_id=course.id,
                user_id=me.user_id,
                role=Role.INSTRUCTOR,
            )
            uow.commit()
        return RedirectResponse(f"/courses/{course.id}", status_code=303)

    @router.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_settings(request: Request, course_id: str, saved: str = "") -> Response:
        """**コース全体**の設定 ── 基本情報・受講者・自動確定・提出形式・採点設定。

        課題（日程・一括確定・追加）はここには出さない。問題セットのページに
        分けてある（`unit_settings`）。1 枚に積むと、教員は「ex03 の締切を
        直す」ために縦に長い画面を目で探すことになる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _course_page(request, me, course, saved=saved)

    def _course_page(
        request: Request,
        me,
        course,
        *,
        saved: str = "",
        note: str | None = None,
        trial=None,
        values=None,
    ) -> Response:
        """コース全体の設定の画面。

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
        people_count = len(enrollments)

        return templates.TemplateResponse(
            request,
            "manage_course.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "コース全体の設定", "href": f"/manage/courses/{course.id}"},
                "saved": note or SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "people_count": people_count,
                "role_counts": _role_counts(enrollments),
                "roles": [role.value for role in Role],
                # シラバスの本文は Markdown。素のまま出すと見出しも箇条書きも
                # 記号のまま並ぶ（課題文で実際に起きた・`statement.py`）。
                "description_html": (
                    render_markdown(course.description) if course.description else None
                ),
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES,
                # 共通ルーブリック。未設定なら組み込みの既定を出して、
                # **いま何が使われているか**を見えるようにする。
                "rubric_rows": rubric.to_rows(
                    rubric.from_stored(course.rubric)
                    if course.rubric
                    else _default_rubric_criteria()
                ),
                "rubric_is_default": not course.rubric,
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
                "languages": sorted(LANGUAGES),
                "trial": trial,
            },
        )

    @router.post("/courses/{course_id}/units")
    def open_unit(
        request: Request,
        course_id: str,
        unit: Annotated[str, Form()] = "",
    ) -> Response:
        """回のページを開く（無ければ空の回として開く）。

        新しい回を作る導線がこれである。**保存は伴わない** ── 回は課題が
        持つ属性であって、それ自体の記録は無い。最初の 1 問を足した時点で
        回が実在する。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        key = quote(unit.strip() or "_", safe="")
        return RedirectResponse(f"/manage/courses/{course_id}/units/{key}", status_code=303)

    @router.get("/courses/{course_id}/units/{unit}", response_class=HTMLResponse)
    def unit_settings(request: Request, course_id: str, unit: str, saved: str = "") -> Response:
        """**1 回ぶん**の設定 ── その回の課題の締切、一括確定、課題の追加。

        まとまりの鍵は `unit`（`overview.unit_key`）。教員が用があるのは
        たいてい「いまの回」で、その単位で開けることが構成の分かりやすさに
        直結する。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        # 課題ごとの未確定件数。**画面に出す。** 自動確定を設定したつもりで
        # cron を仕掛け忘れても、件数が減らないことで気づける。
        pending = pending_counts(console.database, course.id)
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            units = load_units(uow, course, pending=pending, now=now)
        # **知らない鍵でも 404 にしない。** 課題を 1 問も持たない回は
        # 「まだ何も無い回」であって存在しない回ではなく、ここが最初の
        # 1 問を足す場所になる。404 にすると新しい回を作る導線が無くなる。
        key = _normalized_unit(unit)
        group = find_unit(units, key) or empty_unit(key, course)

        rows = []
        for task, version in group.tasks:
            rows.append(
                {
                    "task": task,
                    "version": version,
                    "test_cases": len(version.test_cases),
                    # 自動採点できるか。できない課題は AI 観点だけで、
                    # 教員の確定が前提になる（ADR 0008）。
                    "auto_graded": bool(version.test_cases),
                    "evaluators": sorted(
                        {c.evaluator_id for c in version.criteria if c.evaluator_id}
                    ),
                    "unfinalized": pending.get(task.id, 0),
                    # 訂正フォームの初期値。読みやすさの観点の重みは
                    # 版の中にあるので、そこから取り出す。
                    "readability_weight": next(
                        (c.weight for c in version.criteria if c.code == "readability"), 0.0
                    ),
                    # 訂正フォームで直せるように、いまの観点を行にして渡す。
                    "rubric_rows": rubric.to_rows(version.criteria),
                }
            )

        return templates.TemplateResponse(
            request,
            "manage_unit.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": group.label,
                    "href": f"/manage/courses/{course.id}/units/{group.key}",
                },
                "unit": group,
                "tasks": rows,
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # コースの既定。問題セットで指定しなければこれが効く。
                "course_grace": course.auto_finalize_after_minutes,
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES,
                # 共通ルーブリック。未設定なら組み込みの既定を出して、
                # **いま何が使われているか**を見えるようにする。
                "rubric_rows": rubric.to_rows(
                    rubric.from_stored(course.rubric)
                    if course.rubric
                    else _default_rubric_criteria()
                ),
                "rubric_is_default": not course.rubric,
                # **生成は登録済み KC からの選択だけ**（`aijudge_admin.kc` の
                # 規則 4）。引退したものは選ばせない。
                "kcs": list_for_namespaces(
                    console.database,
                    allowed_namespaces(
                        load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
                    ),
                    include_deprecated=False,
                ),
                "difficulties": [d.value for d in Difficulty],
                # ルーブリックの編集で、観点に指名できる評価器を出す。
                "deterministic": _evaluator_rows(
                    EvaluatorRegistry().load_installed(), EvaluatorKind.DETERMINISTIC
                ),
                "PROVISIONAL": GradeWindow.PROVISIONAL,
                "ELAPSED": GradeWindow.ELAPSED,
                "last_task": (
                    console.last_task[1]
                    if console.last_task is not None and console.last_task[0] == str(course.id)
                    else None
                ),
                "last_finalize": (
                    console.last_finalize[1]
                    if console.last_finalize is not None
                    and console.last_finalize[0] == str(course.id)
                    else None
                ),
            },
        )

    @router.post("/courses/{course_id}/units/{unit}/schedule")
    def set_unit_schedule(
        request: Request,
        course_id: str,
        unit: str,
        opens_at: Annotated[str, Form()] = "",
        submissions_open_at: Annotated[str, Form()] = "",
        due_at: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットの日程。その中の全課題に同じ値を入れる。**

        課題ごとに違う締切を持てると、同じセットの中で締切がずれ、学習者にも
        教員にも「この回はいつまでか」が言えなくなる。日程はセットの性質で
        あって課題の性質ではない。

        締切の判定は `Submission.submitted_at`（提出確定の時刻）で行う。
        ここで入れる値がその基準になる。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={
                "opens_at": _parse_when(opens_at),
                "submissions_open_at": _parse_when(submissions_open_at),
                "due_at": _parse_when(due_at),
            },
            saved="schedule",
        )

    @router.post("/courses/{course_id}/units/{unit}/auto-finalize")
    def set_unit_grace(
        request: Request,
        course_id: str,
        unit: str,
        after_minutes: Annotated[str, Form()] = "",
    ) -> Response:
        """この問題セットの自動確定までの猶予（分）。空ならコースの既定に戻す。

        **日程とは別のフォームにする。** 締切を直しに来たときに猶予まで
        書き換えてしまう（あるいはその逆）事故を、フォームの単位で防ぐ。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"auto_finalize_after_minutes": _parse_minutes(after_minutes)},
            saved="grace",
        )

    @router.post("/courses/{course_id}/units/{unit}/number")
    def set_unit_number(
        request: Request,
        course_id: str,
        unit: str,
        session: Annotated[str, Form()] = "",
    ) -> Response:
        """回番号。**並べ替えと表示のためだけの値**で、採点には効かない。

        日程と混ぜない ── 効き方が違うものを 1 つの保存ボタンにまとめると、
        何が変わったのか教員に分からない。
        """
        number = int(session) if session.strip() else None
        return _update_unit(request, course_id, unit, update={"session": number}, saved="number")

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
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        minutes = _parse_minutes(after_minutes)

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(update={"auto_finalize_after_minutes": minutes})
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=course_grace#course_grace", status_code=303
        )

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

    def _propose(console, course, body: str):
        """本文から候補を作る。名前空間と既存の体系を添えて渡す。"""
        if len(body) < 40:
            raise HTTPException(
                status_code=400,
                detail="シラバスの本文を貼り付けるか、PDF を選んでください（40 文字以上）",
            )
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        existing = [
            kc.key
            for kc in list_for_namespaces(console.database, namespaces, include_deprecated=False)
        ]
        try:
            result = SyllabusReader().propose(
                body, namespaces=namespaces, existing_keys=tuple(existing)
            )
        except Exception as exc:  # 生成の失敗は運用の事象。理由を画面に返す。
            raise HTTPException(
                status_code=502,
                detail=f"候補を作れませんでした（S6 が止まっている可能性があります）: {exc}",
            ) from exc
        return result.proposal, namespaces, existing

    # -- コースの基本情報 --------------------------------------------------

    @router.get("/courses/{course_id}/basics", response_class=HTMLResponse)
    def basics(request: Request, course_id: str, saved: str = "") -> Response:
        from .app import require_principal

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
        from .app import require_principal

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
        from .app import require_principal

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
        return RedirectResponse(f"/manage/courses/{course_id}/basics?saved=basics", status_code=303)

    # -- 知識要素の候補 ----------------------------------------------------

    @router.get("/courses/{course_id}/kc/candidates", response_class=HTMLResponse)
    def kc_candidates(request: Request, course_id: str) -> Response:
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        return templates.TemplateResponse(
            request,
            "manage_kc_candidates.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "知識要素の候補",
                    "href": f"/manage/courses/{course.id}/kc/candidates",
                },
                "namespaces": namespaces,
                "existing": [
                    kc.key
                    for kc in list_for_namespaces(
                        console.database, namespaces, include_deprecated=False
                    )
                ],
                "example": SYLLABUS_EXAMPLE,
                "proposal": None,
            },
        )

    @router.post("/courses/{course_id}/kc/candidates", response_class=HTMLResponse)
    async def propose_kcs(
        request: Request,
        course_id: str,
        text: Annotated[str, Form()] = "",
        upload: UploadFile | None = None,
    ) -> Response:
        """シラバスから知識要素の候補を出す。**登録はしない。**"""
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        payload = await upload.read() if upload is not None and upload.filename else None
        body = _read_body(text, upload, payload)
        proposal, namespaces, existing = _propose(console, course, body)
        return templates.TemplateResponse(
            request,
            "manage_kc_candidates.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "知識要素の候補",
                    "href": f"/manage/courses/{course.id}/kc/candidates",
                },
                "namespaces": namespaces,
                "existing": existing,
                "example": SYLLABUS_EXAMPLE,
                "proposal": proposal,
                "pasted": body,
            },
        )

    @router.post("/courses/{course_id}/kc/adopt")
    def adopt_candidates(
        request: Request,
        course_id: str,
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        label: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
    ) -> Response:
        """選ばれた候補だけを体系に登録する。

        **規則は手で足すときと同じ**（`aijudge_admin.kc`）── 名前空間・親の
        実在・第 1 階層の権限。候補だから緩める、ということはしない。
        親から順に並べて入れるので、`cs.loops` と `cs.loops.termination` を
        同時に選んでも通る。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        labels = dict(zip(kc, label, strict=False))

        failures: list[str] = []
        for key in sorted({k.strip() for k in kc if k.strip()}, key=lambda k: k.count(".")):
            try:
                register_kc(
                    console.database,
                    key=key,
                    label=labels.get(key, "").strip() or key,
                    namespaces=namespaces,
                    actor_id=me.user_id,
                    allow_root=_is_admin(request, me),
                )
            except AdminError as exc:
                failures.append(f"{key}: {exc}")
        if failures:
            raise HTTPException(status_code=400, detail=" / ".join(failures))
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_added#kc", status_code=303
        )

    @router.post("/courses/{course_id}/rubric")
    async def save_course_rubric(request: Request, course_id: str) -> Response:
        """コースの共通ルーブリック。**新しい課題がこれを引き継ぐ。**

        既にある課題は変わらない ── 出題済みの版は書き換えない（P8）。
        個別に直したい課題は、その課題の訂正から観点を宣言する。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        try:
            criteria = rubric.parse(_rubric_from_form(form))
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(update={"rubric": tuple(c.model_dump() for c in criteria)})
            )
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}?saved=rubric#rubric", status_code=303)

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
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        suffixes = normalize_suffixes(suffix)
        if not suffixes:
            raise HTTPException(status_code=400, detail="形式を 1 つ以上選んでください")
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(course.model_copy(update={"upload_suffixes": suffixes}))
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=formats#formats", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/finalize")
    def finalize_remaining(
        request: Request,
        course_id: str,
        task_id: str,
        justification: Annotated[str, Form()] = "",
    ) -> Response:
        """この課題の未確定分をまとめて確定する。

        **根拠説明を必須にする。** 学習者にそのまま表示される。個別に読んで
        いない成績を確定させる操作なので、何を根拠にそうしたのかが残らないと
        学習者は何も分からない（設計原則 P4 を一括操作にも適用する）。

        未対応の異議申立は確定しない。そこは 1 件ずつ読むべきものとして
        待ち行列に残す。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        text = justification.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"確定の根拠を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（学習者に表示されます）"
                ),
            )

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            if task is None or task.course_id != CourseId(course_id):
                raise HTTPException(status_code=404, detail="課題が見つかりません")

        try:
            outcome = finalize_task(
                console.database,
                task_id=TaskId(task_id),
                actor_id=me.user_id,
                justification=text,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 表示のために保持する。**コースを添える**（Console は全利用者で共有で、
        # 添えないと別コースの教員に他コースの課題名が出る）。
        console.last_finalize = (str(course_id), outcome)
        return RedirectResponse(_unit_href(course_id, task), status_code=303)

    @router.post("/courses/{course_id}/units/{unit}/finalize")
    def finalize_unit(
        request: Request,
        course_id: str,
        unit: str,
        justification: Annotated[str, Form()] = "",
    ) -> Response:
        """問題セットの未確定分を、その中の全課題についてまとめて確定する。

        **根拠説明を必須にする。** 学習者にそのまま表示される。個別に読んで
        いない成績を確定させる操作なので、何を根拠にそうしたのかが残らないと
        学習者は何も分からない（設計原則 P4 を一括操作にも適用する）。

        未対応の異議申立は確定しない。そこは 1 件ずつ読むべきものとして
        「再確認の依頼」に残す。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        text = justification.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"確定の根拠を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（学習者に表示されます）"
                ),
            )

        key = _normalized_unit(unit)
        with console.database.unit_of_work() as uow:
            task_ids = [
                task.id
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == key
            ]
        if not task_ids:
            raise HTTPException(status_code=404, detail="この問題セットには課題がありません")

        try:
            outcomes = finalize_tasks(
                console.database,
                task_ids=task_ids,
                actor_id=me.user_id,
                justification=text,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 表示のために保持する。**コースを添える**（Console は全利用者で共有で、
        # 添えないと別コースの教員に他コースの課題名が出る）。
        console.last_finalize = (str(course_id), _merged(outcomes))
        return RedirectResponse(f"/courses/{course_id}/finalize", status_code=303)

    @router.post("/courses/{course_id}/tasks")
    def add_task(
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
    ) -> Response:
        """課題を 1 件足す。

        **保存の中身は API と同じ経路を通る**（`aijudge_admin.save_task`）。
        経路ごとに組み立て方が分かれると、「画面から作った課題だけ観点が
        1 つ足りない」が起きる。実際に起きた ── 廃止した zip 取り込みは
        `readability_weight` が 0.0 固定で、画面から入れた課題には AI 観点が
        付かなかった。

        テストケースはここでは入れない。1 件ずつ貼らせる画面にすると、
        実在する規模（1 課題 7 件 × 48 課題）で現実的でない。まとまった
        投入は API を使う。

        **日程は指定させない。** 問題セットの値をそのまま引き継ぐ ── 課題
        ごとに違う締切を持てると、同じセットの中で締切がずれる。変えたい
        ときはセットの日程を変える（`set_unit_schedule`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        # **キーの前半は問題セットが決める。** 回のページから追加する限り
        # `ex02/p8` の `ex02/` は動かず、教員が打つのは `p8` だけである。
        # 打たせると `ex2/p8` のような取り違えが混ざり、鍵は同一性そのもの
        # なので、取り違えたぶんは別の課題として増える。
        full_key = key.strip() or _compose_key(unit.strip(), key_suffix.strip())
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

        try:
            spec = TaskSpec(
                key=full_key,
                statement=statement,
                unit=unit.strip() or None,
                position=int(position) if position.strip() else None,
                readability_weight=float(readability_weight or 0.0),
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
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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

        console.last_task = (str(course.id), saved)
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{saved.task.id}/edit?saved=task",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/generate")
    def generate_task(
        request: Request,
        course_id: str,
        unit: str,
        key_suffix: Annotated[str, Form()],
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        difficulty: Annotated[str, Form()] = "standard",
        constraints: Annotated[str, Form()] = "",
        test_cases: Annotated[str, Form()] = "5",
        readability_weight: Annotated[str, Form()] = "0.3",
    ) -> Response:
        """AI に課題を 1 つ作らせる。**承認するまで出題されない**（P5）。

        **KC は登録済みからの選択だけ。** モデルはもっともらしいキーを
        いくらでも作るので、自由入力にすると体系が静かに荒れる
        （`aijudge_admin.kc` の規則 4）。

        `avoid_similar_to` にはこのコースの既存課題を入れる ── 「似せない」
        材料が無いと、既存課題の言い換えが出てくる。

        生成物はここでは保存するだけで、門・解答可能性・重複の検査は
        `aijudge-authoring` が担う（ADR 0008）。ここが返すのは候補であって
        課題ではない。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        chosen = tuple(k.strip() for k in kc if k.strip())
        if not chosen:
            raise HTTPException(status_code=400, detail="知識要素を 1 つ以上選んでください")
        try:
            # 選択肢は登録済みから出しているが、直接叩かれる経路もある。
            assert_registered(console.database, chosen)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        key = _normalized_unit(unit)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        with console.database.unit_of_work() as uow:
            siblings = [
                task
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == key
            ]
            # **似せないための材料。** 既存課題の本文を渡す（学習者のデータは
            # 含まないので、外部モデルにも渡してよい・設計原則 P7）。
            avoid = []
            for task in uow.tasks.list_for_course(CourseId(course_id)):
                version = uow.tasks.latest_version(task.id)
                if version is not None:
                    avoid.append(version.statement)

        head = siblings[0] if siblings else None
        full_key = _compose_key(head.unit if head else unit, key_suffix.strip())
        if not full_key:
            raise HTTPException(status_code=400, detail="課題キーを入力してください")

        try:
            blueprint = Blueprint(
                knowledge_components=chosen,
                subject_profile=course.subject_profile,
                difficulty=Difficulty(difficulty),
                language=_language_of(profile),
                constraints=tuple(
                    line.strip() for line in constraints.splitlines() if line.strip()
                ),
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

        spec = result.spec.model_copy(
            update={"readability_weight": float(readability_weight or 0.0)}
        )
        try:
            saved = save_task(
                console.database,
                course_id=course.id,
                spec=spec,
                subject_profile=course.subject_profile,
                authored_by=me.user_id,
                generated_by=result.model,
                generation_prompt_version=result.prompt_id,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 日程と提出形式は問題セットから引き継ぐ（手で足した課題と同じ）。
        if head is not None:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_task(
                    saved.task.model_copy(
                        update={
                            "session": head.session,
                            "opens_at": head.opens_at,
                            "submissions_open_at": head.submissions_open_at,
                            "due_at": head.due_at,
                            "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
                            "accepted_suffixes": head.accepted_suffixes,
                        }
                    )
                )
                uow.commit()

        console.last_task = (str(course.id), saved)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{key}?saved=generated#generate", status_code=303
        )

    def _save_revision(
        console, me, course, task, version, *, statement, criteria, position, accepted
    ):
        """課題を直して新しい版を作る。訂正と「共通に戻す」で共有する。

        課題キーは変えられない ── 同一性の鍵で、変えれば別の課題になる。
        保存済みの版から取り出す（`TaskVersion.source_key`）。
        """
        try:
            spec = TaskSpec(
                key=_key_of(task, version),
                statement=statement,
                unit=task.unit,
                session=task.session,
                position=position,
                # **画面で編集した観点が勝つ。** 観点を宣言する課題では
                # `readability_weight` は使わない（両方書けるとどちらが効くのか
                # 読めない）。
                criteria=criteria,
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
                revise=True,
                course_rubric=course.rubric,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 日程・提出形式は `TaskSpec` を通らないので、ここで書き戻す。
        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(
                saved.task.model_copy(
                    update={
                        "opens_at": task.opens_at,
                        "submissions_open_at": task.submissions_open_at,
                        "due_at": task.due_at,
                        "auto_finalize_after_minutes": task.auto_finalize_after_minutes,
                        "accepted_suffixes": accepted,
                    }
                )
            )
            uow.commit()
        console.last_task = (str(course.id), saved)
        return saved

    def _course_rubric_rows(course):
        """このコースの既定の観点（共通ルーブリック、無ければ組み込み）。"""
        criteria = (
            rubric.from_stored(course.rubric) if course.rubric else _default_rubric_criteria()
        )
        return rubric.to_rows(criteria)

    def _task_page(
        request, me, course, *, unit_key_value, task=None, version=None, note=None, saved=""
    ):
        """課題の編集／追加の画面。**追加と訂正で同じ形を使う。**

        別々に作ると、片方にだけ項目が足りない状態が生まれる（実際に
        `readability_weight` でそうなった）。
        """
        registry = EvaluatorRegistry().load_installed()
        course_rows = _course_rubric_rows(course)
        rows = rubric.to_rows(version.criteria) if version is not None else course_rows
        return templates.TemplateResponse(
            request,
            "manage_task.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": task.title if task is not None else "課題を追加",
                    "href": f"/manage/courses/{course.id}/units/{unit_key_value}",
                },
                "unit_key": unit_key_value,
                "task": task,
                "version": version,
                "rubric_rows": rows,
                # 共通ルーブリックのままか、この課題で変えてあるか。
                # **共通が設定されているときだけ言う** ── 組み込みの既定は
                # 課題の作られ方（テストケースの有無）で中身が変わるので、
                # 「同じ」と言い切れない。
                "course_has_rubric": bool(course.rubric),
                "rubric_is_course_default": bool(course.rubric) and rows == course_rows,
                "deterministic": _evaluator_rows(registry, EvaluatorKind.DETERMINISTIC),
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": (
                    (task.accepted_suffixes if task is not None else ())
                    or course.upload_suffixes
                    or DEFAULT_UPLOAD_SUFFIXES
                ),
                "note": note or SAVED_MESSAGES.get(saved),
            },
        )

    @router.get("/courses/{course_id}/tasks/{task_id}/edit", response_class=HTMLResponse)
    def edit_task(request: Request, course_id: str, task_id: str, saved: str = "") -> Response:
        """既にある課題を直す画面。**問題セットのページには展開しない。**

        ルーブリックと問題文は横幅いっぱいで読むものなので、一覧の中に
        畳んで置くと段階の説明が読めない。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        return _task_page(
            request,
            me,
            course,
            unit_key_value=unit_key(task),
            task=task,
            version=version,
            saved=saved,
        )

    @router.get("/courses/{course_id}/units/{unit}/tasks/new", response_class=HTMLResponse)
    def new_task(request: Request, course_id: str, unit: str) -> Response:
        """課題を追加する画面。訂正と同じ形。"""
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _task_page(request, me, course, unit_key_value=_normalized_unit(unit))

    @router.post("/courses/{course_id}/tasks/{task_id}/rubric/reset")
    def reset_task_rubric(request: Request, course_id: str, task_id: str) -> Response:
        """この課題の観点をコースの共通ルーブリックに戻す。

        **版が上がる。** 出題済みの採点基準は書き換えないので、戻すことも
        新しい版を作ることになる（P8）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        criteria = (
            rubric.from_stored(course.rubric) if course.rubric else _default_rubric_criteria()
        )
        _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=version.statement,
            criteria=rubric.parse(rubric.to_rows(criteria)),
            position=task.position,
            accepted=task.accepted_suffixes,
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/move")
    def move_task(
        request: Request,
        course_id: str,
        task_id: str,
        direction: Annotated[str, Form()] = "up",
    ) -> Response:
        """課題の並びを 1 つ入れ替える。

        **数字を打たせない。** 出題順は「この問題セットの中で何番目か」で
        あって、教員が意識するのは前後関係だけである。数字で持たせると、
        1 問差し込むたびに全部を打ち直すことになる。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            tasks = [
                task
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == unit_key(uow.tasks.get_task(TaskId(task_id)))
            ]
            ordered = sorted(tasks, key=lambda item: item.sort_key)
            index = next((i for i, task in enumerate(ordered) if str(task.id) == task_id), None)
            if index is None:
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            swap = index - 1 if direction == "up" else index + 1
            if 0 <= swap < len(ordered):
                first, second = ordered[index], ordered[swap]
                # 位置を入れ替える。番号が無い課題には並び順から与える。
                first_position = first.position or index + 1
                second_position = second.position or swap + 1
                uow.tasks.save_task(first.model_copy(update={"position": second_position}))
                uow.tasks.save_task(second.model_copy(update={"position": first_position}))
                uow.commit()
            key = unit_key(ordered[index])
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{key}?saved=order", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/revise")
    async def revise_task(request: Request, course_id: str, task_id: str) -> Response:
        """既にある課題を直す。**出題済みの版は書き換えず、版を上げる**（P8）。

        過去の採点がどの基準で付いたのかを辿れなくなるので、上書きはしない。
        内容が同じなら版は上がらない（提出形式だけ変えたい場合がこれ）。

        観点が行数ぶん並ぶので、フォーム全体を読む。
        """
        from .app import require_principal

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
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        # 観点を送ってこない経路（問題文だけ直す等）では、**いまの観点を
        # そのまま引き継ぐ**。空で作り直すと、読みやすさの観点が黙って消えて
        # 次の版から採点されなくなる。
        criteria = criteria or rubric.from_criteria(version.criteria)

        _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=statement,
            criteria=criteria,
            position=int(position) if position.strip() else task.position,
            accepted=_chosen_suffixes(suffix, formats, course),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task", status_code=303
        )

    @router.get("/courses/{course_id}/enrolments", response_class=HTMLResponse)
    def enrolments(request: Request, course_id: str, q: str = "", saved: str = "") -> Response:
        """受講者の一覧。**コースの設定とは別の画面にする。**

        受講 100 名規模になると、設定を 1 つ直しに来た教員が毎回 100 行を
        めくることになる。絞り込みは前方一致 ── 選択肢に並べても選べない
        （提出の一覧と同じ理由）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        prefix = q.strip().lower()
        with console.database.unit_of_work() as uow:
            all_enrollments = uow.identity.list_enrollments(course.id)
            people = []
            for enrollment in all_enrollments:
                user = uow.identity.get_user(enrollment.user_id)
                login = getattr(user, "login", "") or str(enrollment.user_id)
                if prefix and not login.lower().startswith(prefix):
                    continue
                people.append({"enrollment": enrollment, "user": user, "login": login})
        people.sort(key=lambda row: (row["enrollment"].role.value, row["login"]))
        return templates.TemplateResponse(
            request,
            "manage_enrolments.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "受講者",
                    "href": f"/manage/courses/{course.id}/enrolments",
                },
                "people": people,
                # **内訳は絞り込みの前に数える。** 絞り込んだ結果の内訳を出すと、
                # 「TA が 0 名」が登録漏れなのか絞り込みの結果なのか分からない。
                "role_counts": _role_counts(all_enrollments),
                "total": len(all_enrollments),
                "q": q.strip(),
                "roles": [role.value for role in Role],
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
            },
        )

    @router.post("/courses/{course_id}/enrolments/{user_id}/role")
    def set_role(
        request: Request,
        course_id: str,
        user_id: str,
        role: Annotated[str, Form()] = "",
    ) -> Response:
        """受講者の役割を変える。**自分の役割は変えられない。**

        自分を学習者に落とすとそのコースが見えなくなり、戻す手段が無い
        （受講の取り消しと同じ理由）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if UserId(user_id) == me.user_id:
            raise HTTPException(status_code=400, detail="自分の役割は変えられません")
        try:
            new_role = Role(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"役割が不正です: {role!r}") from None

        console = _console(request)
        with console.database.unit_of_work() as uow:
            existing = uow.identity.find_enrollment(CourseId(course_id), UserId(user_id))
            if existing is None:
                raise HTTPException(status_code=404, detail="この受講者は登録されていません")
            uow.identity.save_enrollment(existing.model_copy(update={"role": new_role}))
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=role", status_code=303
        )

    @router.post("/courses/{course_id}/enrolments")
    def add_enrolments(
        request: Request,
        course_id: str,
        roster: Annotated[str, Form()],
        role: Annotated[str, Form()] = Role.LEARNER.value,
    ) -> Response:
        """名簿を貼り付けて受講登録する。

        **既存利用者のパスワードは変えない。** 新規利用者が居る場合は
        パスワードの配布が必要なので、ここでは作らず CLI に回す
        （画面に平文を出すと端末の履歴や画面共有に残る）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        try:
            entries = parse_roster(roster, default_role=Role(role))
        except (RosterError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 画面からは**既存利用者の登録だけ**を許す。新規作成はパスワードの
        # 配布が伴うので CLI（`aijudge-admin enrol --credentials`）で行う。
        with console.database.unit_of_work() as uow:
            unknown = [
                entry.login
                for entry in entries
                if uow.identity.find_user_by_login(me.tenant_id, entry.login) is None
            ]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"利用者が未登録です: {', '.join(unknown[:10])}"
                    f"{' ほか' if len(unknown) > 10 else ''}。"
                    "新規作成はパスワードの配布が伴うため "
                    "`aijudge-admin enrol --credentials <path>` で行ってください"
                ),
            )
        enrol_roster(
            console.database,
            tenant_id=me.tenant_id,
            course_id=CourseId(course_id),
            entries=entries,
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=enrolled#enrolments", status_code=303
        )

    @router.post("/courses/{course_id}/enrolments/{user_id}/remove")
    def remove_enrolment(request: Request, course_id: str, user_id: str) -> Response:
        """受講を取り消す。**利用者は消さない。**"""
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if UserId(user_id) == me.user_id:
            # 自分を外すとそのコースが見えなくなり、戻す手段が無い。
            raise HTTPException(status_code=400, detail="自分の受講は取り消せません")
        console = _console(request)
        with console.database.unit_of_work() as uow:
            uow.identity.remove_enrollment(CourseId(course_id), UserId(user_id))
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=removed", status_code=303
        )

    # ------------------------------------------------------------------
    # 採点設定（コースごとの上書き）
    # ------------------------------------------------------------------

    @router.post("/courses/{course_id}/grading", response_class=HTMLResponse)
    async def save_grading(request: Request, course_id: str) -> Response:
        """保存、または試走。**試走は保存しない。**

        項目が多いので、フォーム全体を読む（宣言した引数では追いつかない）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        registry = EvaluatorRegistry().load_installed()

        form = await request.form()
        overrides = _collect_overrides(form)
        action = str(form.get("action") or "save")

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
                return _course_page(request, me, course, note=str(exc), values=overrides)
            return _course_page(request, me, course, trial=trial, values=overrides)

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
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=grading#grading", status_code=303
        )

    # ------------------------------------------------------------------
    # 知識要素（KC）の体系（設計原則 P6）
    # ------------------------------------------------------------------

    @router.get("/courses/{course_id}/kc", response_class=HTMLResponse)
    def kc_index(request: Request, course_id: str, saved: str = "") -> Response:
        """このコースが使える KC の一覧。

        **コースをまたいで共有される語彙である。** 同じ名前空間を使う他の
        コースにも同じものが見えるので、どれだけ使われているかを添える。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        kcs = list_for_namespaces(console.database, namespaces)
        return templates.TemplateResponse(
            request,
            "manage_kc.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "知識要素", "href": f"/manage/courses/{course.id}/kc"},
                "namespaces": namespaces,
                "rows": [kc_usage(console.database, kcs)[kc.key] for kc in kcs],
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "is_admin": _is_admin(request, me),
            },
        )

    @router.post("/courses/{course_id}/kc")
    def add_kc(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """KC を 1 つ足す。規則の強制は `aijudge_admin.kc` にある。

        **追加は明示的な行為にする**（禁止はしない）。科目の専門家は教員
        しかおらず、禁止すれば既存の近いキーに無理やり寄せられるだけで、
        構造としてはより悪くなる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        try:
            register_kc(
                console.database,
                key=key.strip(),
                label=label.strip() or key.strip(),
                description=description,
                namespaces=allowed_namespaces(profile),
                actor_id=me.user_id,
                # 第 1 階層（分野の根）を作れるのは管理者だけ。
                allow_root=_is_admin(request, me),
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_added#kc", status_code=303
        )

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
        from .app import require_principal

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
        return RedirectResponse(f"/manage/courses/{course_id}/kc?saved={saved}#kc", status_code=303)

    # ------------------------------------------------------------------
    # 生成された課題のレビュー（S2、設計方針 §5）
    # ------------------------------------------------------------------

    @router.get("/courses/{course_id}/drafts", response_class=HTMLResponse)
    def draft_queue(request: Request, course_id: str) -> Response:
        """レビュー待ちの生成課題。

        **科目プロファイルと違い、ここはブラウザから触ってよい**（ADR 0002）。
        あちらは評価器の指名とタイムアウトを持つ採点の設定で、壊すと全員の
        採点が止まる。課題を承認するかどうかは、まさに教員が決めることである。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        rows = []
        with console.database.unit_of_work() as uow:
            tasks = {task.id: task for task in uow.tasks.list_for_course(course.id)}
            for version in uow.tasks.list_versions_in_review():
                if version.task_id not in tasks:
                    continue
                checks = uow.tasks.get_checks(version.id)
                rows.append(
                    {
                        "version": version,
                        "task": tasks[version.task_id],
                        "checks": checks,
                        # 検査していない課題も並べる。**隠さない** ── 見えない
                        # ものは承認も却下もされず、待ち行列に溜まり続ける。
                        "clean": bool(checks and checks.verification.usable),
                    }
                )
        return templates.TemplateResponse(
            request,
            "manage_drafts.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "未承認の課題",
                    "href": f"/manage/courses/{course.id}/drafts",
                },
                "rows": rows,
                "min_reason": MIN_JUSTIFICATION_LENGTH,
            },
        )

    @router.post("/courses/{course_id}/drafts/{version_id}")
    def decide_draft(
        request: Request,
        course_id: str,
        version_id: str,
        decision: Annotated[str, Form()],
        reason: Annotated[str, Form()] = "",
    ) -> Response:
        """承認または却下する。**却下には理由が要る。**

        理由は作問の改善に還流する材料であり、承認率の分母でもある
        （設計方針 §5）。「見た」だけでは何も残らない。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        approved = decision == "approve"
        text = reason.strip()
        if not approved and len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"却下の理由を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（作問の改善に使います）"
                ),
            )

        with console.database.unit_of_work() as uow:
            version = uow.tasks.get_version(TaskVersionId(version_id))
            task = None if version is None else uow.tasks.get_task(version.task_id)
            if version is None or task is None or task.course_id != course.id:
                # 存在と権限を区別しない（他コースの課題を探らせない）。
                raise HTTPException(status_code=404, detail="課題版が見つかりません")
            try:
                uow.tasks.record_review(
                    version.id,
                    approved=approved,
                    reviewer=me.user_id,
                    reason=None if approved else text,
                )
            except ValueError as exc:
                # 二度目のレビュー。やり直しは新しい版から（P8）。
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            uow.commit()

        return RedirectResponse(f"/manage/courses/{course_id}/drafts", status_code=303)

    return router
