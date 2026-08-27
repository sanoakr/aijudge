"""科目・課題・受講の管理（教員向け）。

**YAML の直接編集を運用の前提にしない**（設計方針 §9.2 Phase 2）。学期の頭に
やることは CLI（`aijudge-admin`）でもできるが、学期中に発生する作業
── 締切の設定、受講者の追加、課題の追加 ── を教員がターミナルで行うのは
現実的でない。

**科目プロファイル（`subjects/*.yaml`）は編集させない。** 表示だけする。
あれは評価器の指名とタイムアウトを持つ採点の設定であり、ブラウザから壊せる
ようにすると、1 人の操作で全員の採点が止まる。コードと同じ扱いでレビューを
通す（ADR 0002）。運用者が「いま何が設定されているか」を見られれば十分で、
それがこの画面の役割。

権限は 2 段。
- コースの作成・削除は **ADMIN**
- そのコースの課題・受講の管理は **INSTRUCTOR 以上**（TA には開けない。
  締切や受講の変更は成績に直接効く）
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from aijudge_admin import AdminError, enrol_roster, ensure_course, import_tasks, parse_roster
from aijudge_admin.roster import RosterError
from aijudge_core import Course, Role, Task
from aijudge_core.ids import CourseId, TaskId, UserId
from aijudge_identity import AuthService, PermissionDenied, Principal

# 取り込む zip の上限。課題ディレクトリはテキストとテストケースだけなので小さい。
# 大きいものは事故か攻撃。
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000

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

    @router.post("/courses/{course_id}/tasks")
    async def import_archive(request: Request, course_id: str, archive: UploadFile) -> Response:
        """課題を zip で取り込む。

        サーバ上のパスを入力させない。ブラウザからサーバのファイルシステムを
        指定させると、そこが読み取りの穴になる。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        payload = await archive.read()
        if not payload:
            raise HTTPException(status_code=400, detail="ファイルが空です")
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"アーカイブが大きすぎます（上限 {MAX_ARCHIVE_BYTES} バイト）",
            )

        with TemporaryDirectory() as staging:
            root = Path(staging)
            try:
                _extract(payload, root)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                report = import_tasks(
                    console.database,
                    course_id=CourseId(course_id),
                    directory=_single_root(root),
                    profiles_dir=console.profiles_dir,
                )
            except AdminError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        console.last_import = report  # 表示のために保持する
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

    return router


# -- zip の展開 --------------------------------------------------------------


def _extract(payload: bytes, root: Path) -> None:
    """zip を展開する。**パスの検証を必ず通す。**

    zip の項目名は攻撃者が決められる。`../` や絶対パスを含む項目
    （zip slip）をそのまま書くと、展開先の外にファイルを置ける。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("zip として読めません") from exc

    names = archive.namelist()
    if len(names) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"項目が多すぎます（上限 {MAX_ARCHIVE_ENTRIES}）")

    total = 0
    for info in archive.infolist():
        name = info.filename
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"アーカイブに不正なパスが含まれています: {name!r}")
        target = (root / name).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError(f"アーカイブに不正なパスが含まれています: {name!r}")
        # 展開後の大きさも見る（圧縮爆弾）。
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES * 8:
            raise ValueError("展開後のサイズが大きすぎます")
    archive.extractall(root)


def _single_root(root: Path) -> Path:
    """zip の中身が 1 ディレクトリだけならその中を返す。

    `ex3.zip` を作ると中身が `ex3/p1 ex3/p2` になることも `p1 p2` になることも
    あり、どちらでも取り込めるようにする。
    """
    children = [path for path in root.iterdir() if not path.name.startswith("__MACOSX")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


def _task_row(task: Task, version_count: int) -> dict[str, object]:  # pragma: no cover
    return {"task": task, "versions": version_count}
