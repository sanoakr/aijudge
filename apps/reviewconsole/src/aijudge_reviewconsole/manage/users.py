"""利用者・テナント設定（学内ネットワーク・Google OIDC）・自分のパスワード（段階 4-3）。

テナント管理者の画面で、**コースを知らない**（自分のパスワードだけは本人なら誰でも）。
`manage/__init__.py` の `register()` が、元のルートがあった位置でここの `register` を呼ぶ。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_audit import AuditAction
from aijudge_core import Role, campus_access, parse_cidrs
from aijudge_core.ids import CourseId, UserId
from aijudge_course_admin.roster import generate_password
from aijudge_identity import AuthenticationFailed, AuthService, Principal
from aijudge_identity.network import (
    MAX_CIDRS,
    MAX_NOTE_LENGTH,
    CampusNetworkSettings,
    CampusRange,
)
from aijudge_identity.oidc import DEFAULT_LOGIN_LABEL, LOGIN_LABEL_MAX, OidcSettings

from ..audit_context import recorder_for, source_ip_of
from ..urls import RedirectResponse
from .common import (
    GRANTABLE_ROLES,
    USERS_STEP,
    _console,
    _require_admin,
    _require_grantable,
    _trail,
)
from .messages import SAVED_MESSAGES


def _require_local_account(me: Principal) -> None:
    """ローカルパスワードを持つ利用者であること（#180）。

    存在しない画面として扱う（403 ではなく 404）。権限の問題ではない ──
    SSO 利用者にはローカルパスワードという概念が無い。「権限がありません」は
    「昇格すれば使える」と読めてしまう。
    """
    if me.is_external:
        raise HTTPException(status_code=404, detail="この利用者にパスワードはありません")


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    # -- ローカル利用者の管理（画面から、管理者専用）-----------------------
    #
    # **#127: `admin` にだけ開ける例外。** 利用者の新規作成は
    # `aijudge-admin`（CLI）に限る、という規則（#100 のコメント、
    # `add_enrolments` の docstring）はそのまま ── ここは「画面から
    # 操作できるのが管理者本人に限られるなら、画面共有・端末履歴の
    # リスクは許容できる」という別枠で、一般の教員には開かない。
    #
    # #144 で作成だけの画面から一覧・属性確認・無効化・パスワード再発行へ
    # 広げた。**無効化は削除ではない**（`AuthService.disable` ── 過去の提出と
    # 採点が利用者を参照しているので、消すと成績の履歴が壊れる）。

    @router.get("/users/new", response_class=HTMLResponse)
    def new_user_form(request: Request) -> Response:
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        return templates.TemplateResponse(
            request,
            "manage_new_user.html",
            {"me": me, "trail": _trail(USERS_STEP, ("利用者を作成", None))},
        )

    @router.post("/users")
    def create_user(
        request: Request,
        login: Annotated[str, Form()],
        display_name: Annotated[str, Form()] = "",
        tenant_admin: Annotated[bool, Form()] = False,
    ) -> Response:
        """ローカル利用者を作る。パスワードはここで生成し、**一度だけ**表示する。

        以降どこにも平文を残さない ── `AuthService.issue_token` が API
        トークンでやっているのと同じ約束。コースへの受講登録はここでは
        行わない（既存の受講者一覧画面が、既存利用者を対象にそれをやる）。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        login = login.strip()
        password = generate_password()
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            try:
                principal = auth.register(
                    tenant_id=me.tenant_id,
                    login=login,
                    display_name=display_name.strip() or login,
                    password=password,
                )
            except AuthenticationFailed as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if tenant_admin:
                auth.set_tenant_admin(principal.user_id, admin=True)
            # **平文は記録しない**（画面に一度だけ出すもの・#144）。
            recorder_for(uow, request, me).record(
                AuditAction.USER_CREATED,
                target_type="user",
                target_id=str(principal.user_id),
                summary=f"利用者を作成した（{login}）",
                detail={"login": login, "tenant_admin": bool(tenant_admin)},
            )
            uow.commit()
        return templates.TemplateResponse(
            request,
            "manage_new_user_created.html",
            {
                "me": me,
                "login": login,
                "password": password,
                "tenant_admin": tenant_admin,
                "trail": _trail(USERS_STEP, ("利用者を作成", None)),
            },
        )

    @router.get("/users", response_class=HTMLResponse)
    def user_list(
        request: Request,
        q: str = "",
        local: str = "",
        login_kind: str = "",
        role: str = "",
        saved: str = "",
    ) -> Response:
        """利用者の一覧（#144）。

        絞り込みは前方一致（受講者一覧と同じ作法）。テナントの規模が
        大きくなると、一覧をそのまま読むより ID を打つ方が速くなる。

        **ログイン方式で絞れる**（#326）。パスワードの再発行や無効化の対象に
        なるのはローカル利用者だけなので、その一覧を出せると運用の単位に合う
        （大学アカウントの利用者は Google 側が本人確認を持っている）。逆向き
        ── 大学アカウントだけ ── も要る。片側しか選べない札だったので、
        「SSO で入った人が何人いるか」が数えられなかった。

        **役割でも絞れる**（#326・受講者一覧と同じ）。TA だけ・教員だけを
        見たいとき、学生の中から探すことになっていた。役割はコースごとに
        付くので、ここで見るのは「**いずれかのコースで**その役割を持つ人」で
        ある ── 1 人が教員と TA の両方であることは普通にあり、丸めない
        （`roles_by_user` の注記）。

        `local=1` は `login_kind=local` の旧名。**受け続ける** ── 運用の
        手元に残った URL が黙って全件に戻ると、絞ったつもりの一覧を読む。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        prefix = q.strip()
        kind = login_kind.strip()
        if not kind and local:
            kind = "local"
        if kind not in {"local", "sso"}:
            kind = ""
        # 値は役割の語彙にあるものだけ（無ければ絞らない・受講者一覧と同じ）。
        wanted = role.strip() if role.strip() in {r.value for r in Role} else ""

        with console.database.unit_of_work() as uow:
            users = uow.identity.list_all_users(me.tenant_id)
            roles = uow.identity.roles_by_user(me.tenant_id)
        if prefix:
            users = tuple(user for user in users if user.login.startswith(prefix))
        if kind == "local":
            users = tuple(user for user in users if user.external_id is None)
        elif kind == "sso":
            users = tuple(user for user in users if user.external_id is not None)
        if wanted:
            users = tuple(
                user for user in users if any(r.value == wanted for r in roles.get(user.id, ()))
            )
        return templates.TemplateResponse(
            request,
            "manage_users.html",
            {
                "me": me,
                "trail": _trail(("利用者の一覧", None)),
                "users": users,
                # 行ごとの役割。**役割の語彙の順に並べる** ── 人ごとに順番が
                # 変わると、列を縦に読み比べられない。
                "roles": {
                    user.id: tuple(r for r in Role if r in roles.get(user.id, frozenset()))
                    for user in users
                },
                "q": prefix,
                "login_kind": kind,
                "role": wanted,
                "roles_vocabulary": tuple(Role),
                "filtered": bool(prefix or kind or wanted),
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.get("/users/{user_id}", response_class=HTMLResponse)
    def user_detail(request: Request, user_id: str, saved: str = "") -> Response:
        """1 人の属性と、どのコースにどの役割で居るか（#144）。"""
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                # 他テナントの利用者は「無い」として扱う（存在を漏らさない）。
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            # **テナント管理者は受講登録なしで全コースに届く**（#128）。
            # `AuthService.courses_for` はその場合に全コースを返すので、
            # ここでは使わずに実際の受講登録だけを並べる ── 全コースを
            # 並べると、無い `Enrollment` があるように見えてしまう。
            # 「管理者だからすべてに届く」はテンプレート側で明示する。
            rows: list[dict[str, object]] = []
            for course in uow.identity.list_courses_for_user(me.tenant_id, user.id):
                enrollment = uow.identity.find_enrollment(course.id, user.id)
                rows.append(
                    {"course": course, "role": None if enrollment is None else enrollment.role}
                )
        return templates.TemplateResponse(
            request,
            "manage_user_detail.html",
            {
                "me": me,
                "trail": _trail(USERS_STEP, (user.login, None)),
                "user": user,
                "rows": rows,
                "is_self": user.id == me.user_id,
                # ローカル利用者だけがパスワードを持つ。大学アカウントの人に
                # 再発行を出すと、押しても入り口が変わらないものを見せることになる。
                "is_local": user.external_id is None,
                "roles": [role.value for role in GRANTABLE_ROLES],
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.post("/users/{user_id}/tenant-admin")
    def set_user_tenant_admin(
        request: Request, user_id: str, admin: Annotated[str, Form()] = ""
    ) -> Response:
        """テナント管理者フラグを立てる／外す。

        **コースをまたぐ権限なので、コースの受講者一覧からは配れない**
        （そちらの上限は `GRANTABLE_ROLES`）。ここは管理者専用の画面なので、
        管理者が次の管理者を決められる ── 利用者の作成画面が同じ例外を
        既に開けている（#127）のと同じ扱い。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        if UserId(user_id) == me.user_id:
            # 自分から管理者を外すと、自分では戻せない（無効化と同じ理屈）。
            raise HTTPException(status_code=400, detail="自分自身の管理者権限は変えられません")

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            AuthService(uow.identity, audit=uow.audit).set_tenant_admin(user.id, admin=bool(admin))
            # 権限の変更は成績に届く（管理者はどのコースの成績にも触れる）。
            # **前後の値を書く** ── 「変えた」だけでは、いま管理者なのが
            # この操作の結果なのか元からなのか、後から読めない。
            recorder_for(uow, request, me).record(
                AuditAction.TENANT_ADMIN_CHANGED,
                target_type="user",
                target_id=str(user.id),
                summary=("管理者権限を与えた" if admin else "管理者権限を外した"),
                detail={
                    "login": user.login,
                    "is_tenant_admin": {"before": user.is_tenant_admin, "after": bool(admin)},
                },
            )
            uow.commit()
        saved = "tenant_admin_granted" if admin else "tenant_admin_revoked"
        return RedirectResponse(f"/manage/users/{user_id}?saved={saved}#saved", status_code=303)

    @router.post("/users/{user_id}/courses/{course_id}/role")
    def set_user_course_role(
        request: Request, user_id: str, course_id: str, role: Annotated[str, Form()]
    ) -> Response:
        """この利用者の、そのコースでの役割を変える。

        コースの受講者一覧（`set_role`）と**同じ規則**を通す ── `admin` は
        画面から配れない。入口が 2 つあるので、規則を写さずに同じ関数を呼ぶ。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            new_role = Role(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"役割が不正です: {role!r}") from None
        _require_grantable(new_role)

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            existing = uow.identity.find_enrollment(CourseId(course_id), user.id)
            if existing is None:
                # 受講していないコースの役割はここでは作らない（受講登録は
                # コース側の画面の仕事）。
                raise HTTPException(status_code=404, detail="このコースの受講登録がありません")
            AuthService(uow.identity, audit=uow.audit).enroll(
                tenant_id=me.tenant_id,
                course_id=CourseId(course_id),
                user_id=user.id,
                role=new_role,
            )
            recorder_for(uow, request, me).record(
                AuditAction.ENROLLED,
                target_type="user",
                target_id=str(user.id),
                summary=f"コースでの役割を {new_role.value} に変えた",
                detail={
                    "course_id": str(course_id),
                    "login": user.login,
                    "role": {"before": existing.role.value, "after": new_role.value},
                },
            )
            uow.commit()
        return RedirectResponse(f"/manage/users/{user_id}?saved=role#saved", status_code=303)

    @router.post("/users/{user_id}/disable")
    def disable_user(request: Request, user_id: str) -> Response:
        """利用者を無効化する。**削除ではない**（`AuthService.disable`）。"""
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        if UserId(user_id) == me.user_id:
            # 自分を無効化すると、自分の管理者権限で自分を戻せない
            # （復旧手段が CLI だけになる）。画面からは塞ぐ。
            raise HTTPException(status_code=400, detail="自分自身は無効化できません")

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            AuthService(uow.identity, audit=uow.audit).disable(user.id)
            recorder_for(uow, request, me).record(
                AuditAction.USER_DISABLED,
                target_type="user",
                target_id=str(user.id),
                summary="利用者を無効化した",
                detail={"login": user.login},
            )
            uow.commit()
        return RedirectResponse(f"/manage/users/{user_id}?saved=disabled#saved", status_code=303)

    @router.post("/users/{user_id}/password", response_class=HTMLResponse)
    def reissue_user_password(request: Request, user_id: str) -> Response:
        """パスワードを再発行し、**一度だけ**表示する（#144）。

        `manage_new_user_created.html` が「再発行のみ可能」と書いていた
        その再発行がこれ。平文はレスポンス以外のどこにも残さない。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        password = generate_password()
        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            if user.external_id is not None:
                # 大学アカウントの利用者はパスワードでログインしない。発行しても
                # 使い道が無く、「配ったのに入れない」を生むだけ。
                raise HTTPException(
                    status_code=400,
                    detail="この利用者は大学アカウントでログインします（パスワードはありません）",
                )
            try:
                AuthService(uow.identity, audit=uow.audit).reissue_password(user.id, new=password)
            except AuthenticationFailed as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # **平文は記録しない。** 画面に一度だけ出すためのもので、
            # 消さない場所に写せば「一度だけ」が嘘になる（#144）。
            recorder_for(uow, request, me).record(
                AuditAction.PASSWORD_REISSUED,
                target_type="user",
                target_id=str(user.id),
                summary="パスワードを再発行した",
                detail={"login": user.login},
            )
            uow.commit()
            login = user.login
        return templates.TemplateResponse(
            request,
            "manage_password_reissued.html",
            {
                "me": me,
                "login": login,
                "password": password,
                "user_id": user_id,
                "trail": _trail(
                    USERS_STEP, (login, f"/manage/users/{user_id}"), ("パスワードの再発行", None)
                ),
            },
        )

    # -- Google OIDC 設定（テナント単位、管理者専用、#124）------------------
    #
    # ログイン画面自体の切替え（`/auth/login` `/auth/callback` の実際の配線・
    # 「大学アカウントでログイン」ボタン）は #125 の範囲。ここは管理者が
    # client_id/secret・許可ドメインを設定する画面だけを持つ。**このリポジトリ
    # は公開物なので、特定機関のドメインや値はどこにもハードコードしない**
    # ── 未設定テナントでは #125 のログイン画面が Google ボタンを出さない。

    @router.get("/campus-networks", response_class=HTMLResponse)
    def campus_networks_form(request: Request, saved: str = "") -> Response:
        """学内と見なすアドレス範囲（#333）。**テナント管理者だけ。**

        **いまの接続元と、その判定を一緒に出す。** 範囲は人が調べて書き写す
        値で、書き写しは間違う ── しかも間違いは「試験当日に全員が提出でき
        ない」という形でしか現れない。学内の端末でこの画面を開けば、登録
        すべき値がその場で読めて、保存した設定が自分に効くかも見える。

        **判定は保存済みの値で行う。** 入力欄の中身で判定すると、保存前の
        文字列で「学内です」と出して、保存に失敗しても気づけない。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)
        with console.database.unit_of_work() as uow:
            settings = uow.identity.get_campus_networks(me.tenant_id)
        ranges = () if settings is None else settings.ranges
        cidrs = tuple(r.cidr for r in ranges)
        source = source_ip_of(request)
        return templates.TemplateResponse(
            request,
            "manage_campus_networks.html",
            {
                "me": me,
                "cidrs": cidrs,
                "ranges": ranges,
                "note_max": MAX_NOTE_LENGTH,
                # 1 行 1 件。`#` より後ろがその範囲の注釈（教室・回線）。
                "text": "\n".join(f"{r.cidr}  # {r.note}" if r.note else r.cidr for r in ranges),
                "source_ip": source,
                "access": campus_access(source, cidrs),
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "trail": _trail(("学内ネットワーク", None)),
            },
        )

    @router.post("/campus-networks")
    def save_campus_networks(request: Request, cidrs: Annotated[str, Form()] = "") -> Response:
        """範囲を保存する。**読めない行はその場で突き返す。**

        保存してから「読めないものは無視されました」と言うより、書き直して
        もらうほうがよい ── 無視された行があることに気づかないまま試験を
        迎えるのが、いちばん高くつく形である。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        lines = [line.strip() for line in cidrs.splitlines() if line.strip()]
        if len(lines) > MAX_CIDRS:
            raise HTTPException(status_code=400, detail=f"範囲は {MAX_CIDRS} 件までです")
        # **1 行ずつ確かめる。** `parse_cidrs` は読めるものだけを返す設計
        # なので、ここで数を比べても「どれが読めなかったか」は言えない。
        # 書式は `範囲  # 注釈`。`#` より後ろが、教員が受け付ける場所を選ぶときに
        # 読む説明（どの教室か・無線か有線か）になる。
        entries: list[CampusRange] = []
        bad: list[str] = []
        for line in lines:
            address, _, note = line.partition("#")
            parsed = parse_cidrs([address])
            if not parsed:
                bad.append(line)
                continue
            if len(note.strip()) > MAX_NOTE_LENGTH:
                raise HTTPException(
                    status_code=400,
                    detail=f"注釈は {MAX_NOTE_LENGTH} 文字までです: {address.strip()}",
                )
            entries.append(CampusRange(cidr=str(parsed[0]), note=note.strip()))
        if bad:
            raise HTTPException(
                status_code=400,
                detail="範囲として読めない行があります: " + "、".join(bad[:5]),
            )
        if len({e.cidr for e in entries}) != len(entries):
            raise HTTPException(
                status_code=400, detail="同じ範囲が重複しています（注釈は 1 つにまとめてください）"
            )

        with console.database.unit_of_work() as uow:
            before = uow.identity.get_campus_networks(me.tenant_id)
            uow.identity.save_campus_networks(
                CampusNetworkSettings(tenant_id=me.tenant_id, ranges=tuple(entries))
            )
            # **誰がいつ変えたかを残す。** ここを変えると、誰が提出できるかが
            # 変わる ── 締切と同じ性質の値である（ADR 0013 と同じ理由）。
            recorder_for(uow, request, me).record(
                AuditAction.CAMPUS_NETWORKS_UPDATED,
                target_type="tenant",
                target_id=str(me.tenant_id),
                summary=f"学内ネットワークを変えた（{len(entries)} 件）",
                detail={
                    "before": [
                        {"cidr": r.cidr, "note": r.note}
                        for r in (() if before is None else before.ranges)
                    ],
                    "after": [{"cidr": r.cidr, "note": r.note} for r in entries],
                },
            )
            uow.commit()
        return RedirectResponse("/manage/campus-networks?saved=campus_networks#saved", 303)

    @router.get("/oidc-settings", response_class=HTMLResponse)
    def oidc_settings_form(request: Request, saved: str = "") -> Response:
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)
        with console.database.unit_of_work() as uow:
            settings = uow.identity.get_oidc_settings(me.tenant_id)
        return templates.TemplateResponse(
            request,
            "manage_oidc_settings.html",
            {
                "me": me,
                "settings": settings,
                "saved": bool(saved),
                "default_login_label": DEFAULT_LOGIN_LABEL,
                "trail": _trail(("Google ログイン設定", None)),
            },
        )

    @router.post("/oidc-settings", response_class=HTMLResponse)
    def oidc_settings_save(
        request: Request,
        client_id: Annotated[str, Form()],
        client_secret: Annotated[str, Form()] = "",
        allowed_domains: Annotated[str, Form()] = "",
        issuer: Annotated[str, Form()] = "",
        login_label: Annotated[str, Form()] = "",
    ) -> Response:
        """保存する。

        **`client_secret` は空欄なら既存の値を引き継ぐ。** 画面には保存後
        二度と平文を出さない ── ただし `/manage/users` の生成パスワードの
        「一度だけ表示」とは違い、こちらは管理者自身が入力した値なので、
        常に伏せておくだけでよい（見せ直す約束はしない）。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        domains = tuple(
            sorted(
                {d.strip() for d in allowed_domains.replace(",", "\n").splitlines() if d.strip()}
            )
        )

        with console.database.unit_of_work() as uow:
            existing = uow.identity.get_oidc_settings(me.tenant_id)

            def error(message: str) -> Response:
                return templates.TemplateResponse(
                    request,
                    "manage_oidc_settings.html",
                    {
                        "me": me,
                        "settings": existing,
                        "error": message,
                        "default_login_label": DEFAULT_LOGIN_LABEL,
                        "trail": _trail(("Google ログイン設定", None)),
                    },
                )

            secret = client_secret.strip() or (existing.client_secret if existing else "")
            if not secret:
                return error("client secret を入力してください")
            if not domains:
                return error("許可ドメインを 1 つ以上入力してください")
            # **画面の `maxlength` は検証ではない**（#212）。curl や
            # `maxlength` を無視する客体から 65 字が来ると、開いている
            # unit_of_work の中で `ValidationError` が出て素の 500 になる
            # ── すぐ上で用意している `error()` を通さずに終わってしまう。
            label = login_label.strip()
            if len(label) > LOGIN_LABEL_MAX:
                return error(f"ログインボタンの文言は {LOGIN_LABEL_MAX} 字までです")

            uow.identity.save_oidc_settings(
                OidcSettings(
                    tenant_id=me.tenant_id,
                    client_id=client_id.strip(),
                    client_secret=secret,
                    allowed_domains=domains,
                    issuer=issuer.strip() or "https://accounts.google.com",
                    # **空欄は既定に戻す。** 機関の語彙を入れる欄なので、
                    # 消したときに前の機関名が残り続けてはいけない（#209）。
                    login_label=label or DEFAULT_LOGIN_LABEL,
                )
            )
            uow.commit()
        return RedirectResponse("/manage/oidc-settings?saved=1#saved", status_code=303)

    # -- 自分のパスワードを変える --------------------------------------------
    #
    # `/users/new` と違って**本人なら誰でも**使える（管理者限定にしない）。
    # ローカル利用者（#127 で管理者が作った学習者・教員アカウント）は、
    # 発行時の一度きり表示パスワードのまま使い続けるほかなく、それを変える
    # 手段がどこにも無かった。
    #
    # **SSO 利用者には出さない**（#180）。当初は「無意味だが害も無い」と
    # 出し分けをしなかったが、Google で入った利用者の `password_hash` は
    # 誰も知らない捨て値（`AuthService.login_with_google`）なので、この画面は
    # 「現在のパスワードが違います」しか返しようがない ── 変えられるはずの
    # ものが変えられない、という誤った案内になる。ヘッダのリンクを隠すだけ
    # では境界にならないので、経路の側でも 404 を返す。

    @router.get("/account/password", response_class=HTMLResponse)
    def account_password_form(request: Request) -> Response:
        from ..app import require_principal

        me = require_principal(request)
        _require_local_account(me)
        return templates.TemplateResponse(
            request,
            "manage_account_password.html",
            {"me": me, "trail": _trail(("パスワード変更", None))},
        )

    @router.post("/account/password", response_class=HTMLResponse)
    def account_password_change(
        request: Request,
        current_password: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        new_password_confirm: Annotated[str, Form()],
    ) -> Response:
        from ..app import SESSION_COOKIE, require_principal

        me = require_principal(request)
        _require_local_account(me)
        console = _console(request)

        def error(message: str) -> Response:
            return templates.TemplateResponse(
                request,
                "manage_account_password.html",
                {"me": me, "error": message, "trail": _trail(("パスワード変更", None))},
            )

        # `hash_password`/`change_password` 自体は長さを見ない
        # （生成パスワードは記号を含まないことがある）。ここで最低限だけ弾く。
        if len(new_password) < 12:
            return error("新しいパスワードは 12 文字以上にしてください")
        if new_password != new_password_confirm:
            return error("新しいパスワードが一致しません")

        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            try:
                auth.change_password(me.user_id, current=current_password, new=new_password)
            except AuthenticationFailed as exc:
                return error(str(exc))
            uow.commit()
        # change_password は自分のセッションも含めて全部失効させる
        # （乗っ取られていた場合の復旧手段がこれしかない、が本人の操作でも
        # 例外なく効く）。ログイン画面へ戻し、Cookie も捨てる。
        # ローカル利用者向けの画面（#125）。ログインし直す先も隠し経路。
        response = RedirectResponse("/auth/local?changed=1", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
