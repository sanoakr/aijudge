"""`aijudge-admin` — 学期の頭に要る操作。

    aijudge-admin course create --code network --title "ネットワーク及び演習" \
        --term 2025-後期 --profile cs_network_python
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

from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import DIVISIONS, Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import (
    DEFAULT_TOKEN_DAYS,
    ENV_DEMO_COURSE,
    AuthenticationFailed,
    AuthService,
    demo_course_from_env,
)
from aijudge_persistence import ENV_DATABASE_URL, Database
from aijudge_telemetry import configure_logging

from . import authoring_cli
from .courses import delete_course
from .demo_reset import reset_demo_course
from .demo_seed import seed_demo_course
from .operations import (
    _IMPORTER,
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
from .tasks import clear_unit

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TENANT = "ten_" + "0" * 32
# 科目プロファイルの置き場所。**web / review / worker と同じ場所を指すこと**
# ── CLI だけ別の場所を読むと、コースを作るときに通った宣言で採点されない。
ENV_PROFILES_DIR = "AIJUDGE_PROFILES_DIR"


def _database(args: argparse.Namespace) -> Database:
    return Database.connect(args.database_url, create=args.create_schema)


def _artifact_store(args: argparse.Namespace):
    """提出物の置き場所。**消せるストアを渡すため**（#194）。

    `delete_course` は `StreamingArtifactStore` でないと消さない ── 最小の
    `ArtifactStore` には削除が無い。ここで渡さないと DB の行だけ消えて
    ファイルが残り、**消したつもりでディスクが減らない**。

    環境変数の名前は学習者アプリと同じ（`AIJUDGE_ARTIFACT_DIR`）。別の名前に
    すると、同じ置き場所を 2 通りに指すことになる。
    """
    from aijudge_submission import FilesystemArtifactStore

    return FilesystemArtifactStore(args.artifacts)


def _tenant(args: argparse.Namespace) -> TenantId:
    return TenantId(args.tenant)


# --------------------------------------------------------------------------
# コース
# --------------------------------------------------------------------------


def _cli_audit(uow, tenant_id: TenantId) -> AuditRecorder:
    """CLI からの操作の監査記録（ADR 0016）。

    **操作者は `system` にする。** `aijudge-admin` は認証された主体を持たない
    ── サーバに入れる人が `sudo -u aijudge` で走らせるもので、誰が打ったかを
    ここで知る手立ては無い。分かっているふりをして誰かの id を書くくらいなら、
    「人間の操作者は特定できていない」と正直に残す方がよい（誰が打ったかは
    シェルへの到達権限と journald の側の問題である）。
    """
    return AuditRecorder.for_system(uow.audit, tenant_id=tenant_id)


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


def cmd_course_delete(args: argparse.Namespace) -> int:
    """コースを消す（#156）。

    **規則は `aijudge_admin.courses` にある。** ここで「提出があれば消さない」
    を書き直さない ── 画面（`/manage`）と CLI の両方から使うので、どちらが
    正しいかを問わずに済むよう 1 か所に置いてある。

    **消す前に規模を出して確認を挟む**（`demo reset` と同じ理由）。「本当に
    よいですか」だけでは、何件消えるのか分からないまま押すことになる。

    CLI に入口があるのは、**ブラウザに入れない立場から消せる必要がある**
    ためである ── 画面の削除はテナント管理者の権限で守られているが、サーバ
    に入れる人はそもそもその権限の外側にいる。
    """
    database = _database(args)
    try:
        with database.unit_of_work() as uow:
            course = uow.identity.get_course(CourseId(args.course))
            if course is None:
                print(f"コース {args.course} がありません", file=sys.stderr)
                return 2
            if course.tenant_id != _tenant(args):
                # **テナントをまたいで消さない。** 打ち間違いが他機関のコース
                # に届く経路にしない（`reset_demo_course` と同じ判断）。
                print("このテナントのコースではありません", file=sys.stderr)
                return 2
            submissions = uow.submissions.list_for_course(course.id)
            tasks = uow.tasks.list_for_course(course.id)
            enrolments = uow.identity.list_enrollments(course.id)

        learner = [item for item in submissions if not item.is_trial]
        trials = [item for item in submissions if item.is_trial]
        print(f"コース: {course.title}（{course.code} / {course.term}）")
        print(f"  学習者の提出 {len(learner):4d} 件（1 件でもあれば消せません）")
        print(f"  動作確認の提出 {len(trials):4d} 件（アーティファクトも消えます）")
        print(f"  課題         {len(tasks):4d} 件")
        print(f"  受講登録     {len(enrolments):4d} 件")
        if not args.yes:
            answer = input("消します。よろしいですか [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("中止しました")
                return 1

        result = delete_course(database, course_id=course.id, artifact_store=_artifact_store(args))
    except AdminError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        database.dispose()

    print(
        f"消しました: {result.course.code} / 課題 {result.tasks} 件 / "
        f"動作確認の提出 {result.trial_submissions} 件 / 受講登録 {result.enrolments} 件"
    )
    return 0


def cmd_demo_reset(args: argparse.Namespace) -> int:
    """デモコースを消して作り直す（#194）。

    **消す操作なので、既定では確認を挟む。** 何件消えるのかを先に出して
    から訊く ── 「本当によいですか」だけでは、規模が分からないまま押す
    ことになる。

    `--yes` は自動化のための逃げ道だが、**cron には載せない**（#194 の
    判断）。膨れ上がりはコンソールのコース一覧から見えるので、人が見て
    叩けば足りる。
    """
    demo = demo_course_from_env()
    if demo is None:
        print(f"{ENV_DEMO_COURSE} が設定されていません", file=sys.stderr)
        return 2

    database = _database(args)
    try:
        with database.unit_of_work() as uow:
            course = uow.identity.get_course(demo.course_id)
            if course is None:
                print(f"デモコース {demo.course_id} がありません", file=sys.stderr)
                return 2
            submissions = uow.submissions.list_for_course(demo.course_id)
            tasks = uow.tasks.list_for_course(demo.course_id)
            enrolments = uow.identity.list_enrollments(demo.course_id)

        # **規模を先に出す。** 空振りと 90 件の削除が同じ顔で終わらないように。
        print(f"デモコース: {course.title}（{course.code} / {course.term}）")
        print(f"  提出       {len(submissions):4d} 件（アーティファクトも消えます）")
        print(f"  課題       {len(tasks):4d} 件")
        print(f"  受講登録   {len(enrolments):4d} 件（次のログインで戻ります）")
        if not getattr(args, "yes", False):
            answer = input("消して作り直します。よろしいですか [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("中止しました")
                return 1

        result = reset_demo_course(
            database,
            demo,
            tenant_id=_tenant(args),
            profiles_dir=args.profiles,
            # 課題の作成者。**人ではない**ので、取り込み用の利用者を使う
            # （名簿の取り込みと同じ・`operations._IMPORTER`）。
            authored_by=_IMPORTER,
            artifact_store=_artifact_store(args),
        )
    except AdminError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        database.dispose()

    print(
        f"作り直しました: 提出 {result.submissions} 件 / 課題 {result.tasks} 件 / "
        f"受講登録 {result.enrolments} 件を消し、課題 {result.seeded_tasks} 件を戻しました"
    )
    return 0


def cmd_demo_seed(args: argparse.Namespace) -> int:
    """デモコースを定義から作る（`subjects/demo/course.yaml`）。

    **何度走らせても増えない。** 定義を直して足したときは、もう一度
    これを走らせれば差分だけが入る（`kc seed` と同じ作法）。
    """
    database = _database(args)
    try:
        result = seed_demo_course(
            database,
            tenant_id=_tenant(args),
            profiles_dir=args.profiles,
            authored_by=_IMPORTER,
        )
    except AdminError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        database.dispose()

    print(f"デモコース: {result.course.title}（{result.course.id}）")
    print(f"  課題 {result.tasks} 件")
    print()
    print("使うには、この ID を環境変数に置いてください:")
    print(f"    set -gx AIJUDGE_DEMO_COURSE {result.course.id}")
    print("知識要素の骨格もまだなら:")
    print("    uv run aijudge-admin kc seed --namespace demo")
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
    """教員・TA を作る、または既存の利用者をコースに登録する。

    **パスワードを先に要求しない**（#175）。要るのは新規に作るときだけで、
    それが分かるのは利用者を引いた後である ── 先に弾いていたので、受講登録
    を足すだけの操作でも使い捨ての文字列を書かされ、その値は捨てられていた。
    不足の報告は `create_staff` から `AdminError` として上がってくる。
    """
    password = args.password or os.environ.get("AIJUDGE_ADMIN_PASSWORD")
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
    # **何が起きたかで書き分ける。** 「既存」だけでは、受講登録が足された
    # のか何もされなかったのかが読めない。
    if created:
        print(f"作成: {args.login} / {args.role}")
    elif args.course:
        print(
            f"既存の利用者を受講登録しました: {args.login} / {args.role}"
            "（パスワードは変えていません）"
        )
    else:
        print(f"既存: {args.login} / {args.role}")
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
            auth = AuthService(uow.identity, audit=uow.audit)
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
            # **平文は記録しない。** ここでしか出さない値を、消さない場所へ
            # 写しては意味が無い。残すのは「誰に・何のために・いつまで」。
            _cli_audit(uow, _tenant(args)).record(
                AuditAction.TOKEN_ISSUED,
                target_type="api_token",
                target_id=str(record.id),
                summary=f"API トークンを発行した（{args.login} / {record.note}）",
                detail={
                    "user_id": str(user.id),
                    "login": args.login,
                    "note": record.note,
                    "expires_at": None
                    if record.expires_at is None
                    else record.expires_at.isoformat(),
                },
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
            tokens = AuthService(uow.identity, audit=uow.audit).list_tokens(_tenant(args))
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
            AuthService(uow.identity, audit=uow.audit).revoke_token(ApiTokenId(args.id))
            _cli_audit(uow, _tenant(args)).record(
                AuditAction.TOKEN_REVOKED,
                target_type="api_token",
                target_id=str(args.id),
                summary="API トークンを失効させた",
            )
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


def cmd_kc_seed(args: argparse.Namespace) -> int:
    """骨格を投入する（`subjects/kc/<名前空間>.yaml`）。

    **何度走らせても増えない。** 骨格ファイルを直して足したときは、
    もう一度これを走らせれば差分だけが入る。
    """
    from .kc import seed as seed_kcs
    from .kc_skeleton import load_skeleton, skeleton_dir

    path = args.file or (skeleton_dir(args.profiles) / f"{args.namespace}.yaml")
    if not path.exists():
        print(f"骨格ファイルがありません: {path}", file=sys.stderr)
        return 1

    database = _database(args)
    try:
        skeleton = load_skeleton(path)
        report = seed_kcs(database, skeleton, namespaces=(skeleton.namespace,))
        with database.unit_of_work() as uow:
            _cli_audit(uow, _tenant(args)).record(
                AuditAction.PROFILE_UPDATED,
                target_type="kc_namespace",
                target_id=skeleton.namespace,
                summary=(
                    f"知識要素の骨格を投入した（新規 {len(report.added)} / "
                    f"既存 {len(report.existing)}）"
                ),
                detail={"source": skeleton.source, "file": str(path)},
            )
            uow.commit()
    except AdminError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()

    print(report.summary())
    if report.added:
        print()
        print("新しく入ったもの:")
        for key in report.added[:20]:
            print(f"  {key}")
        if len(report.added) > 20:
            print(f"  … ほか {len(report.added) - 20} 件")
    return 0


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


def cmd_unit_clear(args: argparse.Namespace) -> int:
    """問題セットを丸ごと片付ける。**内訳を必ず出す**（#59）。

    1 回の操作で課題ごとに結果が違う（削除か取り下げか）ので、件数だけを
    出すと何がどうなったのか分からない。課題キーまで並べる。
    """
    database = _database(args)
    try:
        report = clear_unit(
            database,
            course_id=CourseId(args.course),
            unit=args.unit,
            dry_run=args.dry_run,
        )
    finally:
        database.dispose()

    label = "削除する" if args.dry_run else "削除した"
    for task in report.deleted:
        print(f"{label}: {task.title}")
    label = "取り下げる" if args.dry_run else "取り下げた"
    for task in report.withdrawn:
        print(f"{label}: {task.title}（提出があるため消しません）")
    for task in report.untouched:
        print(f"変更なし: {task.title}（既に取り下げ済み）")

    print(
        f"\n合計 {report.total} 件 / 削除 {len(report.deleted)} 件 / "
        f"取り下げ {len(report.withdrawn)} 件 / 変更なし {len(report.untouched)} 件"
    )
    if args.dry_run:
        print("（dry-run のため何も変えていません）")
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
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path(os.environ.get(ENV_PROFILES_DIR, REPO_ROOT / "subjects")).expanduser(),
        help="科目プロファイルの置き場所（既定はリポジトリの subjects/ ── これはサンプル）",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(
            os.environ.get("AIJUDGE_ARTIFACT_DIR", Path.home() / ".aijudge" / "artifacts")
        ).expanduser(),
        help="提出物の置き場所（デモコースのリセットで消すのに要る）",
    )
    parser.add_argument("--create-schema", action="store_true", help="開発用")
    sub = parser.add_subparsers(dest="command", required=True)

    course = sub.add_parser("course", help="コース").add_subparsers(
        dest="course_command", required=True
    )
    create = course.add_parser("create", help="作成（既にあれば更新）")
    create.add_argument("--code", required=True, help="コースコード（例: network）")
    create.add_argument("--title", required=True)
    create.add_argument(
        "--term",
        required=True,
        # **選べる値を語彙から書く**（#167）。書き写すと、区分を増やした
        # 日にヘルプだけが古い一覧を出す。
        help=f"学期（`年度-区分`。例: 2026-前期。区分は {'・'.join(DIVISIONS)}）",
    )
    create.add_argument("--profile", required=True, help="科目プロファイル名")
    create.set_defaults(func=cmd_course_create)
    course.add_parser("list", help="一覧").set_defaults(func=cmd_course_list)
    # 削除は**課題があっても消えるが、学習者の提出があれば消えない**
    # （`aijudge_admin.courses`）。画面にも同じ操作がある（#156）。
    course_delete = course.add_parser(
        "delete", help="消す（学習者の提出が 1 件でもあれば消さない）"
    )
    course_delete.add_argument("--course", required=True, help="コース ID")
    course_delete.add_argument("--yes", action="store_true", help="確認を省く")
    course_delete.set_defaults(func=cmd_course_delete)

    # デモコースのリセット（#194）。**`course` の下に置かない** ── 対象は
    # 環境変数が指す 1 つに固定されており、コースを選べる操作ではない。
    demo = sub.add_parser("demo", help="デモコース").add_subparsers(
        dest="demo_command", required=True
    )
    demo_reset = demo.add_parser(
        "reset", help="消して作り直す（提出・受講登録が消える。問題セットは入れ直す）"
    )
    demo_reset.add_argument(
        "--yes", action="store_true", help="確認を省く（**cron には載せないこと**）"
    )
    demo_reset.set_defaults(func=cmd_demo_reset)
    demo.add_parser("seed", help="定義から作る（冪等。subjects/demo/course.yaml）").set_defaults(
        func=cmd_demo_seed
    )

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

    kc = sub.add_parser("kc", help="知識要素").add_subparsers(dest="kc_command", required=True)
    seed_p = kc.add_parser("seed", help="骨格を投入する（分野と単位はここからしか作れない）")
    seed_p.add_argument("--namespace", required=True, help="名前空間（例 cs）")
    seed_p.add_argument(
        "--file",
        type=Path,
        default=None,
        help="骨格ファイル（既定は <profiles>/kc/<名前空間>.yaml）",
    )
    seed_p.set_defaults(func=cmd_kc_seed)

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
    unit = sub.add_parser("unit", help="問題セット").add_subparsers(
        dest="unit_command", required=True
    )
    clear = unit.add_parser(
        "clear", help="問題セットを丸ごと片付ける（未使用は削除・使用済みは取り下げ）"
    )
    clear.add_argument("--course", required=True)
    clear.add_argument("--unit", required=True, help="問題セットの名前（例: ex03）")
    clear.add_argument("--dry-run", action="store_true", help="何もせず内訳だけ出す")
    clear.set_defaults(func=cmd_unit_clear)

    # 作問とレビュー（S2）。**別モジュールに置く** ── このファイルは既に
    # コース・受講・トークンを持っており、作問まで足すと何のための CLI か
    # 読めなくなる。
    authoring_cli.register(task)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 一括操作は成績に届く。何をしたかが journald に残るようにしておく
    # （**誰が何を変えたか**の記録は監査ログ側の仕事 ── ADR 0016）。
    configure_logging("admin")
    try:
        return int(args.func(args))
    except AdminError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
