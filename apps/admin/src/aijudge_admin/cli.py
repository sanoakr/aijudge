"""`aijudge-admin` — 学期の頭に要る操作。

    aijudge-admin course create --code network --title "ネットワーク及び演習" \
        --term 2025-後期 --profile net_python
    aijudge-admin enrol --course <id> --roster 2025shj-user.txt --credentials ~/pw.tsv
    aijudge-admin task import --course <id> --dir .../sharif-judge/ex3
    aijudge-admin course list
    aijudge-admin task list --course <id>

**すべて冪等。** 学期の頭に何度も流し直すもので、2 回目で落ちたり、
既存利用者のパスワードが再生成されて配った紙が無効になったりしては使えない。

パスワードは**ファイルにだけ**書き出す（0600）。標準出力に出すと端末の履歴・
ログ・画面共有に残る。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from aijudge_core import Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import DEFAULT_TOKEN_DAYS, AuthenticationFailed, AuthService
from aijudge_persistence import ENV_DATABASE_URL, Database

from .operations import (
    AdminError,
    create_staff,
    enrol_roster,
    ensure_course,
    import_tasks,
    list_courses,
    list_tasks,
    set_password,
)
from .roster import RosterError, load_roster, write_credentials

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TENANT = "ten_" + "0" * 32


def _database(args: argparse.Namespace) -> Database:
    return Database.connect(args.database_url, create=args.create_schema)


def _tenant(args: argparse.Namespace) -> TenantId:
    return TenantId(args.tenant)


# --------------------------------------------------------------------------
# コース
# --------------------------------------------------------------------------


def cmd_course_create(args: argparse.Namespace) -> int:
    database = _database(args)
    try:
        course, created = ensure_course(
            database,
            tenant_id=_tenant(args),
            code=args.code,
            title=args.title,
            term=args.term,
            subject_profile=args.profile,
            profiles_dir=args.profiles,
        )
    finally:
        database.dispose()
    print(f"{'作成' if created else '更新'}: {course.id}")
    print(f"  {course.code} / {course.title} / {course.term} / {course.subject_profile}")
    return 0


def cmd_course_list(args: argparse.Namespace) -> int:
    database = _database(args)
    try:
        courses = list_courses(database, _tenant(args))
    finally:
        database.dispose()
    if not courses:
        print("コースがありません")
        return 0
    print(f"{'ID':38s} {'コード':12s} {'学期':12s} {'プロファイル':16s} 題名")
    for course in courses:
        print(
            f"{course.id:38s} {course.code:12s} {course.term:12s} "
            f"{course.subject_profile:16s} {course.title}"
        )
    return 0


# --------------------------------------------------------------------------
# 受講者
# --------------------------------------------------------------------------


def cmd_enrol(args: argparse.Namespace) -> int:
    try:
        entries = load_roster(args.roster, default_role=Role(args.role))
    except RosterError as exc:
        print(f"名簿が読めません: {exc}", file=sys.stderr)
        return 1

    database = _database(args)
    try:
        report = enrol_roster(
            database,
            tenant_id=_tenant(args),
            course_id=CourseId(args.course),
            entries=entries,
            dry_run=args.dry_run,
        )
    finally:
        database.dispose()

    print(f"名簿 {len(entries)} 名")
    print(f"  新規作成: {len(report.created)}")
    print(f"  受講登録のみ: {len(report.enrolled)}")
    print(f"  変更なし: {len(report.already)}")

    if report.created and not args.dry_run:
        if args.credentials is None:
            # 出力先が無いのに作ってしまうと、誰もログインできない利用者が残る。
            print(
                "\n新規利用者のパスワードを書き出す先が指定されていません。"
                "--credentials <path> を付けて再実行してください"
                "（既存利用者はそのままです）。",
                file=sys.stderr,
            )
            return 1
        write_credentials(args.credentials, report.created)
        print(f"\nパスワードを書き出しました: {args.credentials}（権限 0600）")
        print("配布したら削除してください。再取得はできません（再発行になります）。")
    elif report.created and args.dry_run:
        print("\n（dry-run のため何も保存していません）")
    return 0


def cmd_staff(args: argparse.Namespace) -> int:
    password = args.password or os.environ.get("AIJUDGE_ADMIN_PASSWORD")
    if not password:
        print(
            "パスワードを --password か AIJUDGE_ADMIN_PASSWORD で渡してください",
            file=sys.stderr,
        )
        return 1
    database = _database(args)
    try:
        created = create_staff(
            database,
            tenant_id=_tenant(args),
            login=args.login,
            display_name=args.name or args.login,
            password=password,
            course_id=None if args.course is None else CourseId(args.course),
            role=Role(args.role),
        )
    finally:
        database.dispose()
    print(f"{'作成' if created else '既存'}: {args.login} / {args.role}")
    return 0


# --------------------------------------------------------------------------
# API トークン
# --------------------------------------------------------------------------


def cmd_token_issue(args: argparse.Namespace) -> int:
    """トークンを発行する。**平文はこの一度だけ表示する。**

    保存するのはハッシュなので、あとから取り出す方法は無い。パスワードを
    ファイルにだけ書き出すのと違い標準出力に出しているのは、これが人ではなく
    エージェントに渡す値で、渡し先が端末とは限らないため。ログに残ることは
    承知の上で、接頭辞（`aij_`）で気づけるようにし、失効を用意してある。
    """
    database = _database(args)
    try:
        with database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            user = uow.identity.find_user_by_login(_tenant(args), args.login)
            if user is None:
                print(f"利用者が見つかりません: {args.login}", file=sys.stderr)
                return 1
            record, token = auth.issue_token(
                tenant_id=_tenant(args),
                user_id=user.id,
                note=args.note,
                days=None if args.days == 0 else args.days,
            )
            uow.commit()
    except (ValueError, AuthenticationFailed) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()

    expiry = "無期限" if record.expires_at is None else record.expires_at.date().isoformat()
    print(f"ID: {record.id}")
    print(f"利用者: {args.login} / 用途: {record.note} / 有効: {expiry}")
    print()
    print(token)
    print()
    print("この平文はここでしか出ません。控えたら画面を閉じてください。")
    return 0


def cmd_token_list(args: argparse.Namespace) -> int:
    database = _database(args)
    try:
        with database.unit_of_work() as uow:
            tokens = AuthService(uow.identity).list_tokens(_tenant(args))
            users = {t.user_id: uow.identity.get_user(t.user_id) for t in tokens}
    finally:
        database.dispose()
    if not tokens:
        print("トークンはありません")
        return 0
    for record in tokens:
        user = users.get(record.user_id)
        state = "失効" if record.revoked_at is not None else "有効"
        used = "未使用" if record.last_used_at is None else record.last_used_at.date().isoformat()
        print(
            f"{record.id}  {state}  {user.login if user else record.user_id}"
            f"  最終使用 {used}  {record.note}"
        )
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    from aijudge_core.ids import ApiTokenId

    database = _database(args)
    try:
        with database.unit_of_work() as uow:
            AuthService(uow.identity).revoke_token(ApiTokenId(args.id))
            uow.commit()
    finally:
        database.dispose()
    print(f"失効させました: {args.id}")
    return 0


def cmd_password(args: argparse.Namespace) -> int:
    from .roster import generate_password

    password = args.password or generate_password()
    database = _database(args)
    try:
        set_password(database, tenant_id=_tenant(args), login=args.login, password=password)
    finally:
        database.dispose()
    write_credentials(args.credentials, [(args.login, password)])
    print(f"再発行しました: {args.login}")
    print(f"パスワードの書き出し先: {args.credentials}（権限 0600）")
    print("既存のセッションは切れています。")
    return 0


# --------------------------------------------------------------------------
# 課題
# --------------------------------------------------------------------------


def cmd_task_import(args: argparse.Namespace) -> int:
    database = _database(args)
    try:
        report = import_tasks(
            database,
            course_id=CourseId(args.course),
            directory=args.dir,
            profiles_dir=args.profiles,
            readability_weight=args.readability_weight,
            evaluator_id=args.evaluator,
            require_test_cases=args.require_test_cases,
            dry_run=args.dry_run,
        )
    finally:
        database.dispose()

    for task in report.imported:
        mark = "" if task.auto_graded else "  ← 自動テストなし（AI 観点のみ）"
        print(f"取り込み: {task.key:16s} テスト {task.test_cases:3d} 件  {task.title}{mark}")
    if report.skipped:
        print(f"\n取り込まなかった課題: {len(report.skipped)} 件", file=sys.stderr)
        for key, reason in report.skipped:
            print(f"  {key}: {reason}", file=sys.stderr)

    print(f"\n合計 {len(report.imported)} 件取り込み / {len(report.skipped)} 件除外")
    if report.review_only:
        # 「取り込めた」と「自動採点できる」は違う。混ぜて報告しない。
        print(
            f"うち {len(report.review_only)} 件は自動テストがまだ無く、"
            "AI 観点のみで採点されます（教員の確定が前提）。"
        )
    if args.dry_run:
        print("（dry-run のため何も保存していません）")
    # 除外があっても取り込めた分は成立している。件数で判断できるよう 0 を返す。
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    database = _database(args)
    try:
        rows = list_tasks(database, CourseId(args.course))
    finally:
        database.dispose()
    if not rows:
        print("課題がありません")
        return 0
    print(f"{'課題版 ID':38s} {'版':>3s} {'テスト':>6s} 題名")
    for task, version in rows:
        print(f"{version.id:38s} {version.version:3d} {len(version.test_cases):6d} {task.title}")
    return 0


# --------------------------------------------------------------------------
# 組み立て
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aijudge-admin", description="コース・受講者・課題の管理（すべて冪等）"
    )
    parser.add_argument("--database-url", default=os.environ.get(ENV_DATABASE_URL))
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="テナント ID（単独運用では既定）")
    parser.add_argument("--profiles", type=Path, default=REPO_ROOT / "subjects")
    parser.add_argument("--create-schema", action="store_true", help="開発用")
    sub = parser.add_subparsers(dest="command", required=True)

    course = sub.add_parser("course", help="コース").add_subparsers(
        dest="course_command", required=True
    )
    create = course.add_parser("create", help="作成（既にあれば更新）")
    create.add_argument("--code", required=True, help="科目コード（例: network）")
    create.add_argument("--title", required=True)
    create.add_argument("--term", required=True, help="学期（例: 2025-後期）")
    create.add_argument("--profile", required=True, help="科目プロファイル名")
    create.set_defaults(func=cmd_course_create)
    course.add_parser("list", help="一覧").set_defaults(func=cmd_course_list)

    enrol = sub.add_parser("enrol", help="名簿からまとめて受講登録")
    enrol.add_argument("--course", required=True)
    enrol.add_argument("--roster", type=Path, required=True, help="名簿ファイル")
    enrol.add_argument(
        "--role",
        default=Role.LEARNER.value,
        choices=[role.value for role in Role],
        help="名簿に役割が書かれていない行の既定",
    )
    enrol.add_argument(
        "--credentials",
        type=Path,
        default=None,
        help="生成したパスワードの書き出し先（新規利用者があるときは必須）",
    )
    enrol.add_argument("--dry-run", action="store_true")
    enrol.set_defaults(func=cmd_enrol)

    staff = sub.add_parser("staff", help="教員・TA を作る")
    staff.add_argument("--login", required=True)
    staff.add_argument("--name", default=None)
    staff.add_argument("--password", default=None, help="未指定なら AIJUDGE_ADMIN_PASSWORD")
    staff.add_argument("--course", default=None)
    staff.add_argument(
        "--role",
        default=Role.INSTRUCTOR.value,
        choices=[role.value for role in Role],
    )
    staff.set_defaults(func=cmd_staff)

    password = sub.add_parser("password", help="パスワードを再発行する")
    password.add_argument("--login", required=True)
    password.add_argument("--password", default=None, help="未指定なら生成する")
    password.add_argument("--credentials", type=Path, required=True)
    password.set_defaults(func=cmd_password)

    token = sub.add_parser("token", help="API トークン").add_subparsers(
        dest="token_command", required=True
    )
    tissue = token.add_parser("issue", help="発行する（平文はこの一度だけ表示）")
    tissue.add_argument("--login", required=True, help="このトークンが名乗る利用者")
    tissue.add_argument("--note", required=True, help="用途（何のためのトークンか）")
    tissue.add_argument(
        "--days",
        type=int,
        default=DEFAULT_TOKEN_DAYS,
        help=f"有効日数（既定 {DEFAULT_TOKEN_DAYS}。0 で無期限）",
    )
    tissue.set_defaults(func=cmd_token_issue)
    token.add_parser("list", help="一覧（平文は出ない）").set_defaults(func=cmd_token_list)
    trevoke = token.add_parser("revoke", help="失効させる")
    trevoke.add_argument("--id", required=True, help="トークン ID（token list で確認）")
    trevoke.set_defaults(func=cmd_token_revoke)

    task = sub.add_parser("task", help="課題").add_subparsers(dest="task_command", required=True)
    imp = task.add_parser("import", help="Sharif Judge の課題ディレクトリを取り込む")
    imp.add_argument("--course", required=True)
    imp.add_argument("--dir", type=Path, required=True, help="問題ディレクトリかその親")
    imp.add_argument(
        "--readability-weight",
        type=float,
        default=0.0,
        help="AI 評価器が担当する「読みやすさ」の重み（0 なら AI 観点なし）",
    )
    imp.add_argument("--evaluator", default=None, help="決定的評価器の ID を明示する")
    imp.add_argument(
        "--require-test-cases",
        action="store_true",
        help=(
            "テストケースが 0 件の課題を拒否する"
            "（取り込み対象を間違えたことに気づくための安全装置）"
        ),
    )
    imp.add_argument("--dry-run", action="store_true")
    imp.set_defaults(func=cmd_task_import)
    tlist = task.add_parser("list", help="一覧")
    tlist.add_argument("--course", required=True)
    tlist.set_defaults(func=cmd_task_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AdminError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
