"""受講者（名簿・役割）と習熟度（段階 4-4）。

名簿と、その結果としての習熟度。受講者の管理は担当教員の権限で、TA には開かない
（`_require_enrolment_manager`）。`manage/__init__.py` の `register()` が、元のルートが
あった位置でここの `register` を呼ぶ。
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_core import Course, Role
from aijudge_core.ids import CourseId, UserId
from aijudge_course_admin.operations import enrol_roster
from aijudge_course_admin.roster import RosterEntry, RosterError, parse_roster
from aijudge_identity import INSTRUCTOR_ROLES, AuthService, PermissionDenied, Principal

from .. import mastery
from ..urls import RedirectResponse
from .common import (
    GRANTABLE_ROLES,
    _console,
    _course_kcs,
    _is_admin,
    _require_grantable,
    _require_instructor,
    _role_counts,
)
from .messages import SAVED_MESSAGES

# 受講登録の結果に載せる「未登録アカウント」の上限。それ以上は件数だけ言う。
MAX_UNKNOWN_SHOWN = 20


def _is_sso_login(login: str, domains: set[str]) -> bool:
    """学内ログイン（OIDC）のアカウントか ── 許可ドメインのメールアドレス。"""
    local, at, domain = login.rpartition("@")
    return bool(at and local) and domain.lower() in {d.lower() for d in domains}


def _require_enrolment_manager(
    request: Request, me: Principal, course_id: CourseId
) -> tuple[Course, Role]:
    """受講者の役割を変えられる者。**そのコースの教員か、テナント全体の管理者。**

    `_require_instructor` はコースの受講登録を要求するが、管理者は
    「指定されたコースに限定した教員を昇格させる」ことができる必要があり、
    昇格させる前のコースにまだ自分が受講登録されているとは限らない（#126）。
    ここだけ、コースの一員でない管理者も通す。

    返す役割は「付与者としての役割」── 管理者はそのコースの実際の受講が
    `learner` や無所属でも `Role.ADMIN` として扱う。`_require_grantable`
    が上限を決めるときの基準になる。
    """
    console = _console(request)
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            role = auth.require_membership(course_id, me.user_id)
        except PermissionDenied:
            role = None
        course = uow.identity.get_course(course_id)

    if role in INSTRUCTOR_ROLES:
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        return course, role
    if _is_admin(request, me):
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        return course, Role.ADMIN
    if role is None:
        # 存在と権限を区別しない（コースを列挙させない）。
        raise HTTPException(status_code=404, detail="コースが見つかりません")
    # 受講はしているが役割が足りない（例: TA）。ここは存在を隠す理由が無い。
    raise HTTPException(status_code=403, detail="この操作には担当教員の権限が必要です")


def _where_evidence_came_from(uow, states) -> tuple[dict, dict]:
    """根拠の採点がどのコースの、どの課題から来たかを解決する。

    **`packages/skill` にコースを持ち込まない。** 習熟度はテナント単位で
    積み上がる値で、コースを知る必要が無い（P6）── 知る必要があるのは
    この画面だけなので、composition root であるここで辿る。

    辿りは 採点 → 課題版 → 課題 → コース の 3 段。**同じ版を二度引かない**
    ── 根拠は 1 KC あたり最大 20 件あり、同じ課題から来ることが多い。
    """
    course_of: dict[str, str | None] = {}
    title_of: dict[str, str | None] = {}
    version_cache: dict[str, tuple[str | None, str | None]] = {}
    for state in states:
        for item in state.evidence:
            run_id = str(item.grading_run_id)
            if run_id in course_of:
                continue
            run = uow.runs.get(item.grading_run_id)
            if run is None:
                course_of[run_id] = None
                title_of[run_id] = None
                continue
            version_id = str(run.context.task_version_id)
            if version_id not in version_cache:
                version = uow.tasks.get_version(run.context.task_version_id)
                task = None if version is None else uow.tasks.get_task(version.task_id)
                version_cache[version_id] = (
                    None if task is None else str(task.course_id),
                    None if task is None else task.title,
                )
            course_of[run_id], title_of[run_id] = version_cache[version_id]
    return course_of, title_of


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.get("/courses/{course_id}/mastery", response_class=HTMLResponse)
    def mastery_overview(request: Request, course_id: str) -> Response:
        """コースの習熟度 ── 分布と推移（#328）。

        **教員とテナント管理者だけ**（`_require_enrolment_manager`）。TA は
        採点を分担するが履修の管理はしない ── 習熟度は成績から積み上がる値で、
        締切や受講の変更と同じ側にある。

        **母数を偽らない。** 受講者の数と、記録のある受講者の数は別に出す。
        まだ誰も問われていない KC は 0 点として数えない ── 数えると「この科目は
        全然できていない」という読みになる（`mastery.overview`）。

        **これは断面である。** 習熟度は学習者 × KC で 1 つの値で、コース別には
        持っていない。ここで「コースの分布」と呼んでいるのは、このコースの
        受講者を、このコースが使う KC で切ったものにすぎない ── 値そのものには
        担当外の科目で得た観測も入っている。画面にそう書く。
        """
        from ..app import require_principal

        me = require_principal(request)
        course, _ = _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            learners = tuple(
                enrollment.user_id
                for enrollment in uow.identity.list_enrollments(course.id)
                if enrollment.role is Role.LEARNER
            )
            kcs = tuple((str(kc.id), kc.key, kc.label) for kc in _course_kcs(console, course))
            # **まとめて引く**（#328）。1 人ずつ引くと受講者数ぶんの往復になる。
            states = uow.skills.list_states_for(me.tenant_id, learners)
            points = uow.skills.history(me.tenant_id, learners)

        view = mastery.overview(states, points, kcs=kcs, learners=len(learners))
        return templates.TemplateResponse(
            request,
            "manage_mastery.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "習熟度",
                    "href": f"/manage/courses/{course.id}/mastery",
                },
                "view": view,
                "band_label": mastery.band_label,
                # 図の寸法はテンプレートに書かない ── 点の座標を作るのが
                # サーバ側なので、同じ値を 2 か所に置くと片方だけずれる。
                "chart": {"width": 640, "height": 140},
                "line": mastery.polyline(view.trend, width=640, height=140),
            },
        )

    @router.get("/courses/{course_id}/mastery/{user_id}", response_class=HTMLResponse)
    def mastery_for_learner(request: Request, course_id: str, user_id: str) -> Response:
        """1 人の習熟度と、その根拠（#328）。

        **根拠はこのコースのものだけを開く。** 習熟度はコースを跨いで動くので、
        1 つの値に担当外の科目の観測が入っている ── 黙って混ぜると、教員は
        自分の課題では説明できない値を説明しようとすることになる。外から来た
        ぶんは**件数だけ**出す（担当していないコースの課題名は成績に近い）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course, _ = _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)

        learner_id = UserId(user_id)
        with console.database.unit_of_work() as uow:
            if uow.identity.find_enrollment(course.id, learner_id) is None:
                # 存在と権限を区別しない（他コースの受講者を列挙させない）。
                raise HTTPException(status_code=404, detail="この受講者は見つかりません")
            learner = uow.identity.get_user(learner_id)
            labels = {str(kc.id): (kc.key, kc.label) for kc in _course_kcs(console, course)}
            states = [
                state
                for state in uow.skills.list_states(me.tenant_id, learner_id)
                if str(state.kc_id) in labels
            ]
            course_of, title_of = _where_evidence_came_from(uow, states)

        rows = [
            mastery.split_evidence(
                state,
                course_of=course_of,
                title_of=title_of,
                course_id=str(course.id),
                key=labels[str(state.kc_id)][0],
                label=labels[str(state.kc_id)][1],
            )
            for state in states
        ]
        rows.sort(key=lambda row: row.key)
        return templates.TemplateResponse(
            request,
            "manage_mastery_learner.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "習熟度",
                    "href": f"/manage/courses/{course.id}/mastery",
                },
                "learner": learner,
                "rows": rows,
            },
        )

    @router.get("/courses/{course_id}/enrolments", response_class=HTMLResponse)
    def enrolments(
        request: Request,
        course_id: str,
        q: str = "",
        role: str = "",
        saved: str = "",
        enrolled: int = 0,
        already: int = 0,
        provisioned: int = 0,
        unknown: str = "",
    ) -> Response:
        """受講者の一覧。**コースの設定とは別の画面にする。**

        受講 100 名規模になると、設定を 1 つ直しに来た教員が毎回 100 行を
        めくることになる。絞り込みは前方一致 ── 選択肢に並べても選べない
        （提出の一覧と同じ理由）。

        `enrolled` / `already` / `unknown` は直前の受講登録の結果
        （`add_enrolments` がリダイレクトで渡す）。**何件入って何件が
        入らなかったかを、その場で言う。** 「保存しました」だけだと、
        名簿の半分が未登録だったことに学期が始まってから気づく。
        """
        from ..app import require_principal

        me = require_principal(request)
        course, _ = _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)

        prefix = q.strip().lower()
        # 役割でも絞る。TA だけ・教員だけを見たいとき、100 名の学生の中から
        # 探すことになっていた。値は役割の語彙にあるものだけ（無ければ絞らない）。
        wanted = role.strip() if role.strip() in {r.value for r in Role} else ""
        with console.database.unit_of_work() as uow:
            oidc = uow.identity.get_oidc_settings(me.tenant_id)
            all_enrollments = uow.identity.list_enrollments(course.id)
            people = []
            for enrollment in all_enrollments:
                if wanted and enrollment.role.value != wanted:
                    continue
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
                "role_filter": wanted,
                "all_roles": [r.value for r in Role],
                # **画面から配れる役割だけを出す**（#100）。`admin` は出さない
                # ── コースをまたぐ権限なので、コースの受講者一覧からは配れない。
                "roles": [role.value for role in GRANTABLE_ROLES],
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                # **アカウントの書き方は設定から出す。** 学内ログイン（OIDC）の
                # アカウントは `<学籍番号>@<許可ドメイン>` で、ローカルアカウント
                # は login そのもの。「学籍番号を並べる」と書いていたが、龍大では
                # 学籍番号＠ドメインがアカウントなので、番号だけを貼ると全員が
                # 未登録になる。
                "login_domains": list(oidc.allowed_domains) if oidc else [],
                "enrol_result": (
                    {
                        "enrolled": enrolled,
                        "already": already,
                        "provisioned": provisioned,
                        "unknown": [name for name in unquote(unknown).split(",") if name],
                    }
                    if saved == "enrolled"
                    else None
                ),
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
        from ..app import require_principal

        me = require_principal(request)
        _require_enrolment_manager(request, me, CourseId(course_id))
        if UserId(user_id) == me.user_id:
            raise HTTPException(status_code=400, detail="自分の役割は変えられません")
        try:
            new_role = Role(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"役割が不正です: {role!r}") from None
        # **画面で塞ぐだけにしない。** 選択肢を減らしても、POST は手で作れる。
        _require_grantable(new_role)

        console = _console(request)
        with console.database.unit_of_work() as uow:
            existing = uow.identity.find_enrollment(CourseId(course_id), UserId(user_id))
            if existing is None:
                raise HTTPException(status_code=404, detail="この受講者は登録されていません")
            # **管理者の役割は画面から動かせない**（#100）。付けられない権限を
            # 外せるのはおかしい ── 担当教員が管理者を自分のコースから締め出せる
            # ことになる。
            if existing.role is Role.ADMIN:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "管理者の役割はこの画面からは変えられません"
                        "（`aijudge-admin` で行ってください）"
                    ),
                )
            uow.identity.save_enrollment(existing.model_copy(update={"role": new_role}))
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=role#saved", status_code=303
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
        from ..app import require_principal

        me = require_principal(request)
        _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)
        try:
            entries = parse_roster(roster, default_role=Role(role))
        except (RosterError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # **名簿の行にも役割が書ける**（`parse_roster` の 4 列目）。既定の役割
        # だけを見ると、貼り付けた名簿の中の `admin` が通る（#100）。
        _require_grantable(Role(role))
        for entry in entries:
            _require_grantable(entry.role)

        # **学内ログインのアカウントは、まだ無ければここで作る**（#285）。
        # login が OIDC の許可ドメインのメールアドレスなら、パスワードを配る
        # 必要が無い ── 捨て値で作っておき、本人の初回 SSO ログインで結び付く。
        # 教員が名簿を貼るだけで学期を始められるのは、これがあってこそ。
        #
        # ローカルアカウントの新規作成だけは引き続き CLI に回す。パスワードの
        # 配布が伴い、画面に平文を出すと端末の履歴や画面共有に残る。
        #
        # **未登録が混ざっていても、残りは入れる。** 全部を断ると、100 行の
        # 名簿が 1 行の綴り違いで丸ごと弾かれ、教員はどの行かを探して貼り
        # 直すことになる。入った件数と入らなかったアカウントは戻り先の画面に
        # そのまま出す（`enrolments` の `enrol_result`）。
        provisioned = 0
        with console.database.unit_of_work() as uow:
            oidc = uow.identity.get_oidc_settings(me.tenant_id)
            domains = set(oidc.allowed_domains) if oidc else set()
            auth = AuthService(uow.identity, audit=uow.audit)
            known: list[RosterEntry] = []
            for entry in entries:
                if uow.identity.find_user_by_login(me.tenant_id, entry.login) is not None:
                    known.append(entry)
                elif _is_sso_login(entry.login, domains):
                    auth.provision_external(tenant_id=me.tenant_id, email=entry.login)
                    provisioned += 1
                    known.append(entry)
            uow.commit()
        unknown = [entry.login for entry in entries if entry not in known]
        enrolled = already = 0
        if known:
            report = enrol_roster(
                console.database,
                tenant_id=me.tenant_id,
                course_id=CourseId(course_id),
                entries=known,
            )
            enrolled, already = len(report.enrolled), len(report.already)
        # 未登録の一覧は URL に載せて戻す。**長さは切る** ── 名簿を丸ごと
        # 間違えた（番号だけを貼った）ときに、URL が数千文字になる。
        shown = ",".join(unknown[:MAX_UNKNOWN_SHOWN])
        if len(unknown) > MAX_UNKNOWN_SHOWN:
            shown += f",…ほか {len(unknown) - MAX_UNKNOWN_SHOWN} 件"
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=enrolled"
            f"&enrolled={enrolled}&already={already}&provisioned={provisioned}"
            f"&unknown={quote(shown)}#result",
            status_code=303,
        )

    @router.post("/courses/{course_id}/enrolments/{user_id}/remove")
    def remove_enrolment(request: Request, course_id: str, user_id: str) -> Response:
        """受講を取り消す。**利用者は消さない。**"""
        from ..app import require_principal

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
            f"/manage/courses/{course_id}/enrolments?saved=removed#saved", status_code=303
        )
