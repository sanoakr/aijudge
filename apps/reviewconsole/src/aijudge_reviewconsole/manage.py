"""科目・課題・受講の管理（教員向け）。

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

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from aijudge_admin import (
    AdminError,
    enrol_roster,
    ensure_course,
    finalize_task,
    parse_roster,
    pending_counts,
    save_task,
)
from aijudge_admin.roster import RosterError
from aijudge_authoring import TaskSpec
from aijudge_core import (
    MIN_JUSTIFICATION_LENGTH,
    Course,
    GradeWindow,
    Role,
    deadline_for,
    grade_window,
)
from aijudge_core.ids import CourseId, TaskId, TaskVersionId, UserId
from aijudge_identity import AuthService, PermissionDenied, Principal

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
    def index(request: Request) -> Response:
        from .app import require_principal

        me = require_principal(request)
        console = _console(request)
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            rows = []
            is_admin = False
            for course in auth.courses_for(me.tenant_id, me.user_id):
                enrollment = uow.identity.find_enrollment(course.id, me.user_id)
                if enrollment is None:
                    continue
                if enrollment.role is Role.ADMIN:
                    is_admin = True
                if enrollment.role in (Role.INSTRUCTOR, Role.ADMIN):
                    rows.append(course)
        profiles = sorted(path.stem for path in console.profiles_dir.glob("*.yaml"))
        return templates.TemplateResponse(
            request,
            "manage_index.html",
            {"me": me, "courses": rows, "is_admin": is_admin, "profiles": profiles},
        )

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
        return RedirectResponse(f"/manage/courses/{course.id}", status_code=303)

    @router.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_detail(request: Request, course_id: str) -> Response:
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        # 課題ごとの未確定件数。**画面に出す。** 自動確定を設定したつもりで
        # cron を仕掛け忘れても、件数が減らないことで気づける。
        pending = pending_counts(console.database, course.id)
        now = datetime.now(UTC)

        with console.database.unit_of_work() as uow:
            tasks = []
            for task in uow.tasks.list_for_course(course.id):
                version = uow.tasks.latest_version(task.id)
                if version is None:
                    continue
                tasks.append(
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
                        # 自動確定が走る時刻。締切か猶予が無ければ None。
                        "auto_finalize_at": deadline_for(
                            task.due_at, course.auto_finalize_after_hours
                        ),
                        # いまどの段階か。**期限経過なのに未確定が残っている
                        # ことが見えるようにする** ── 自動確定が動いていないか、
                        # 教員の対応を待っているものがあるかのどちらかである。
                        "window": grade_window(
                            task.due_at, course.auto_finalize_after_hours, now
                        ),
                    }
                )
            enrollments = uow.identity.list_enrollments(course.id)
            members = {
                user.id: user
                for user in uow.identity.list_users(
                    me.tenant_id,
                    tuple(),  # login では引けないので下で個別に引く
                )
            }
            people = []
            for enrollment in enrollments:
                user = members.get(enrollment.user_id) or uow.identity.get_user(enrollment.user_id)
                people.append({"enrollment": enrollment, "user": user})

        # 科目プロファイルは**表示だけ**。編集させない（モジュール docstring 参照）。
        profile_path = console.profiles_dir / f"{course.subject_profile}.yaml"
        profile_text = profile_path.read_text(encoding="utf-8") if profile_path.is_file() else None
        return templates.TemplateResponse(
            request,
            "manage_course.html",
            {
                "me": me,
                "course": course,
                "tasks": tasks,
                "people": sorted(people, key=lambda row: row["enrollment"].role.value),
                "profile_text": profile_text,
                "profile_path": profile_path.name,
                "roles": [role.value for role in Role],
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # このコースの結果だけを渡す。
                "PROVISIONAL": GradeWindow.PROVISIONAL,
                "ELAPSED": GradeWindow.ELAPSED,
                "last_task": (
                    console.last_task[1]
                    if console.last_task is not None
                    and console.last_task[0] == str(course.id)
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

    @router.post("/courses/{course_id}/tasks/{task_id}/schedule")
    def set_schedule(
        request: Request,
        course_id: str,
        task_id: str,
        opens_at: Annotated[str, Form()] = "",
        due_at: Annotated[str, Form()] = "",
    ) -> Response:
        """公開日時と締切を設定する。

        締切の判定は `Submission.submitted_at`（提出確定の時刻）で行う。
        ここで入れる値がその基準になる。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        opens = _parse_when(opens_at)
        due = _parse_when(due_at)
        if opens is not None and due is not None and due <= opens:
            raise HTTPException(status_code=400, detail="締切が公開日時より前になっています")

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            if task is None or task.course_id != CourseId(course_id):
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            uow.tasks.save_task(task.model_copy(update={"opens_at": opens, "due_at": due}))
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}", status_code=303)

    @router.post("/courses/{course_id}/auto-finalize")
    def set_auto_finalize(
        request: Request,
        course_id: str,
        after_hours: Annotated[str, Form()] = "",
    ) -> Response:
        """締切から成績を自動確定するまでの猶予（時間）。

        空なら自動確定しない。**既定はそれ**で、教員が明示的に入れて初めて
        自動確定が始まる。既定で自動確定させると、設定を知らない教員の
        コースで成績が勝手に閉じる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        text = after_hours.strip()
        hours: float | None = None
        if text:
            try:
                hours = float(text)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"時間の形式が不正です: {after_hours!r}"
                ) from None
            if hours <= 0:
                # 0 を許すと締切と同時に確定し、締切直前の提出が採点前に
                # 確定しうる。猶予は正の値でなければ意味がない。
                raise HTTPException(status_code=400, detail="猶予は 0 より大きい値にしてください")

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(update={"auto_finalize_after_hours": hours})
            )
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}", status_code=303)

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
        return RedirectResponse(f"/manage/courses/{course_id}", status_code=303)

    @router.post("/courses/{course_id}/tasks")
    def add_task(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
        statement: Annotated[str, Form()],
        unit: Annotated[str, Form()] = "",
        session: Annotated[str, Form()] = "",
        position: Annotated[str, Form()] = "",
        readability_weight: Annotated[str, Form()] = "0.3",
        due_at: Annotated[str, Form()] = "",
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
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        try:
            spec = TaskSpec(
                key=key.strip(),
                statement=statement,
                unit=unit.strip() or None,
                session=int(session) if session.strip() else None,
                position=int(position) if position.strip() else None,
                due_at=_parse_when(due_at),
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
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        console.last_task = (str(course.id), saved)
        return RedirectResponse(f"/manage/courses/{course_id}", status_code=303)

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
        return RedirectResponse(f"/manage/courses/{course_id}", status_code=303)

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
        return RedirectResponse(f"/manage/courses/{course_id}", status_code=303)

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
