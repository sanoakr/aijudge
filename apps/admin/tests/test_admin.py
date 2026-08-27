"""管理操作の規則を固定する。

固定したいのは 3 つ。

冪等   学期の頭に何度流し直しても同じ結果になる。2 回目で落ちない。
不変   既存利用者のパスワードを再生成しない（配った紙が無効になる）。
漏らさない  パスワードを標準出力に出さない。ファイルは 0600。
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from aijudge_admin import (
    AdminError,
    RosterError,
    create_staff,
    enrol_roster,
    ensure_course,
    import_tasks,
    list_courses,
    list_tasks,
    parse_roster,
    set_password,
    write_credentials,
)
from aijudge_admin.cli import main
from aijudge_core import Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import AuthService, verify_password
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
TENANT = TenantId("ten_" + "0" * 32)

# 移行元に実在する形式（network2025/2025shj-user.txt）。
SHARIF_ROSTER = """\
t190054 t190054@mail.ryukoku.ac.jp RANDOM[8] student
y230012 y230012@mail.ryukoku.ac.jp RANDOM[8] student
sano sano@math.ryukoku.ac.jp RANDOM[12] instructor
"""

# login だけの形式（network2025/2025stdlist.txt）。
PLAIN_ROSTER = "t190054\ny230012\n"


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    obj, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-前期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    return obj


# --------------------------------------------------------------------------
# 名簿
# --------------------------------------------------------------------------


def test_the_sharif_judge_roster_format_is_read() -> None:
    """移行元に実在するファイルをそのまま読む。"""
    entries = parse_roster(SHARIF_ROSTER)
    assert [e.login for e in entries] == ["t190054", "y230012", "sano"]
    assert entries[0].email == "t190054@mail.ryukoku.ac.jp"
    assert entries[0].role is Role.LEARNER
    assert entries[2].role is Role.INSTRUCTOR
    assert entries[2].password_length == 12


def test_a_plain_login_list_is_read() -> None:
    entries = parse_roster(PLAIN_ROSTER)
    assert [e.login for e in entries] == ["t190054", "y230012"]
    assert all(e.role is Role.LEARNER for e in entries)


def test_random_placeholders_do_not_become_passwords() -> None:
    """`RANDOM[8]` を平文パスワードとして受け取らない。"""
    entries = parse_roster(SHARIF_ROSTER)
    assert all(e.password is None for e in entries)


def test_a_duplicate_login_is_refused() -> None:
    """重複を黙って 1 人にすると、受講者が 1 人足りないまま学期が始まる。"""
    with pytest.raises(RosterError, match="重複"):
        parse_roster("t190054 a@b.c RANDOM[8] student\nt190054 a@b.c RANDOM[8] student\n")


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(RosterError, match="役割"):
        parse_roster("t190054 a@b.c RANDOM[8] wizard\n")


def test_a_short_password_length_is_refused() -> None:
    with pytest.raises(RosterError, match="短すぎ"):
        parse_roster("t190054 a@b.c RANDOM[4] student\n")


def test_an_empty_roster_is_refused() -> None:
    with pytest.raises(RosterError, match="有効な行がありません"):
        parse_roster("# コメントだけ\n\n")


def test_comments_and_blank_lines_are_ignored() -> None:
    entries = parse_roster("# 2025 後期\n\nt190054\n\n")
    assert len(entries) == 1


def test_generated_passwords_avoid_confusable_characters() -> None:
    """手で配る前提なので 0/O・1/l/I を混ぜない。"""
    from aijudge_admin import generate_password

    for _ in range(50):
        assert not set(generate_password(20)) & set("0O1lI")


# --------------------------------------------------------------------------
# 資格情報の書き出し
# --------------------------------------------------------------------------


def test_credentials_are_written_with_restrictive_permissions(tmp_path: Path) -> None:
    """端末の履歴やログに残さないためファイルに書く。読めるのは所有者だけ。"""
    path = tmp_path / "creds" / "pw.tsv"
    write_credentials(path, [("t190054", "abcdefgh")])
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert "t190054\tabcdefgh" in path.read_text(encoding="utf-8")
    assert "削除" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# コース
# --------------------------------------------------------------------------


def test_creating_the_same_course_twice_updates_it(database: Database) -> None:
    """学期の頭に何度流し直しても同じコースになる。"""
    first, created_first = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク及び演習",
        term="2025-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    second, created_second = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク及び演習（改）",
        term="2025-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    assert first.id == second.id
    assert (created_first, created_second) == (True, False)
    assert second.title == "ネットワーク及び演習（改）"
    assert len(list_courses(database, TENANT)) == 1


def test_a_different_term_is_a_different_course(database: Database) -> None:
    a, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク",
        term="2025-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    b, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク",
        term="2026-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    assert a.id != b.id


def test_an_unknown_subject_profile_is_refused(database: Database) -> None:
    """存在しないプロファイルでコースを作ると、提出は通るのに採点が恒久的に失敗する。"""
    with pytest.raises(AdminError, match="科目プロファイル"):
        ensure_course(
            database,
            tenant_id=TENANT,
            code="math",
            title="微分積分",
            term="2026-前期",
            subject_profile="no_such_profile",
            profiles_dir=PROFILES,
        )


# --------------------------------------------------------------------------
# 受講登録
# --------------------------------------------------------------------------


def test_the_roster_becomes_users_and_enrolments(database: Database, course) -> None:
    report = enrol_roster(
        database,
        tenant_id=TENANT,
        course_id=course.id,
        entries=parse_roster(SHARIF_ROSTER),
    )
    assert len(report.created) == 3
    assert report.total == 3

    with database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        user = uow.identity.find_user_by_login(TENANT, "sano")
        assert user is not None
        assert auth.role_in(course.id, user.id) is Role.INSTRUCTOR
        learner = uow.identity.find_user_by_login(TENANT, "t190054")
        assert learner is not None
        assert auth.role_in(course.id, learner.id) is Role.LEARNER


def test_the_generated_password_actually_works(database: Database, course) -> None:
    report = enrol_roster(
        database, tenant_id=TENANT, course_id=course.id, entries=parse_roster(PLAIN_ROSTER)
    )
    login, password = report.created[0]
    with database.unit_of_work() as uow:
        principal, token = AuthService(uow.identity).login(
            tenant_id=TENANT, login=login, password=password
        )
    assert principal.login == login
    assert token


def test_re_enrolling_does_not_reset_passwords(database: Database, course) -> None:
    """配ったパスワードが名簿の流し直しで無効になってはならない。"""
    first = enrol_roster(
        database, tenant_id=TENANT, course_id=course.id, entries=parse_roster(PLAIN_ROSTER)
    )
    login, password = first.created[0]

    second = enrol_roster(
        database, tenant_id=TENANT, course_id=course.id, entries=parse_roster(PLAIN_ROSTER)
    )
    assert second.created == []
    assert sorted(second.already) == ["t190054", "y230012"]

    with database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, login)
        assert user is not None
        assert verify_password(password, user.password_hash), "パスワードが再生成された"


def test_a_role_change_is_applied(database: Database, course) -> None:
    """学生 → TA の昇格は反映する。"""
    enrol_roster(database, tenant_id=TENANT, course_id=course.id, entries=parse_roster("y230012\n"))
    report = enrol_roster(
        database,
        tenant_id=TENANT,
        course_id=course.id,
        entries=parse_roster("y230012 y230012@example.jp RANDOM[8] ta\n"),
    )
    assert report.enrolled == ["y230012"]
    with database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "y230012")
        assert user is not None
        assert AuthService(uow.identity).role_in(course.id, user.id) is Role.ASSISTANT


def test_a_dry_run_changes_nothing(database: Database, course) -> None:
    report = enrol_roster(
        database,
        tenant_id=TENANT,
        course_id=course.id,
        entries=parse_roster(PLAIN_ROSTER),
        dry_run=True,
    )
    assert len(report.created) == 2
    with database.unit_of_work() as uow:
        assert uow.identity.find_user_by_login(TENANT, "t190054") is None


def test_enrolling_into_an_unknown_course_is_refused(database: Database) -> None:
    with pytest.raises(AdminError, match="コース"):
        enrol_roster(
            database,
            tenant_id=TENANT,
            course_id=CourseId("crs_" + "f" * 32),
            entries=parse_roster(PLAIN_ROSTER),
        )


def test_reissuing_a_password_replaces_it_and_cuts_sessions(database: Database, course) -> None:
    """平文を保存していないので、忘れた学生への正しい操作は再発行。"""
    report = enrol_roster(
        database, tenant_id=TENANT, course_id=course.id, entries=parse_roster("y230012\n")
    )
    _, old = report.created[0]
    with database.unit_of_work() as uow:
        _, token = AuthService(uow.identity).login(tenant_id=TENANT, login="y230012", password=old)
        uow.commit()

    set_password(database, tenant_id=TENANT, login="y230012", password="a-brand-new-password")

    with database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        assert auth.resolve(token) is None, "セッションが残っている"
        assert auth.login(tenant_id=TENANT, login="y230012", password="a-brand-new-password")


def test_staff_can_be_created_without_a_course(database: Database) -> None:
    assert create_staff(
        database,
        tenant_id=TENANT,
        login="sano",
        display_name="SANO Akira",
        password="a-long-password",
    )
    assert not create_staff(
        database,
        tenant_id=TENANT,
        login="sano",
        display_name="SANO Akira",
        password="a-long-password",
    )


# --------------------------------------------------------------------------
# 課題の取り込み
# --------------------------------------------------------------------------


def test_a_problem_directory_is_imported(database: Database, course) -> None:
    report = import_tasks(
        database, course_id=course.id, directory=EXAMPLE_TASK, profiles_dir=PROFILES
    )
    assert len(report.imported) == 1
    task = report.imported[0]
    assert task.test_cases == 5
    assert task.title
    assert task.auto_graded

    rows = list_tasks(database, course.id)
    assert len(rows) == 1


def test_importing_twice_is_idempotent(database: Database, course) -> None:
    """同じ内容の再取り込みで課題が増えない。"""
    import_tasks(database, course_id=course.id, directory=EXAMPLE_TASK, profiles_dir=PROFILES)
    import_tasks(database, course_id=course.id, directory=EXAMPLE_TASK, profiles_dir=PROFILES)
    assert len(list_tasks(database, course.id)) == 1


def test_changing_the_statement_is_refused(database: Database, course, tmp_path: Path) -> None:
    """問題文を直したら版を上げる。過去の採点基準を書き換えない（P8）。"""
    staged = tmp_path / "ex1" / "p1"
    staged.parent.mkdir(parents=True)
    shutil.copytree(EXAMPLE_TASK, staged)
    import_tasks(database, course_id=course.id, directory=staged, profiles_dir=PROFILES)

    (staged / "desc.md").write_text("## 別の問題\n\n書き換えた問題文\n", encoding="utf-8")
    with pytest.raises(AdminError, match="版を上げる"):
        import_tasks(database, course_id=course.id, directory=staged, profiles_dir=PROFILES)


def test_a_parent_directory_imports_every_problem(
    database: Database, course, tmp_path: Path
) -> None:
    """運用者が手元のディレクトリをそのまま渡せること（ex3/ に p1 p2 p3）。"""
    assignment = tmp_path / "ex3"
    for name in ("p1", "p2", "p3"):
        shutil.copytree(EXAMPLE_TASK, assignment / name)
    report = import_tasks(
        database, course_id=course.id, directory=assignment, profiles_dir=PROFILES
    )
    assert sorted(task.key for task in report.imported) == ["ex3/p1", "ex3/p2", "ex3/p3"]
    assert len(list_tasks(database, course.id)) == 3


def _server_task(tmp_path: Path) -> Path:
    """自動テストの無い課題。実在する形（in/out が空のサーバ課題）。"""
    staged = tmp_path / "ex5" / "p1"
    staged.mkdir(parents=True)
    (staged / "desc.md").write_text("## httpServer2.py\n\nサーバを作りなさい\n", encoding="utf-8")
    (staged / "in").mkdir()
    (staged / "out").mkdir()
    return staged


def test_a_task_without_test_cases_is_imported(database: Database, course, tmp_path: Path) -> None:
    """自動テストが無い課題も取り込む。

    HTTP サーバ課題（受動的に応答するので Sharif Judge では判定できなかった）、
    自己採点課題、レポート課題が実在する。これらは「自動採点できない」のでは
    なく「**まだ**自動採点できない」課題である。
    """
    report = import_tasks(
        database, course_id=course.id, directory=_server_task(tmp_path), profiles_dir=PROFILES
    )
    assert len(report.imported) == 1
    assert report.imported[0].test_cases == 0
    assert not report.imported[0].auto_graded
    assert len(list_tasks(database, course.id)) == 1


def test_a_task_without_test_cases_is_graded_by_the_ai_not_left_unscored(
    database: Database, course, tmp_path: Path
) -> None:
    """決定的評価器に担当させない。

    担当させると、その観点は永久に採点されず全提出が review_required で
    教員に積まれる。AI 観点にしておけば採点は成立し、教員が確定させる。
    """
    import_tasks(
        database, course_id=course.id, directory=_server_task(tmp_path), profiles_dir=PROFILES
    )
    (_task, version) = list_tasks(database, course.id)[0]
    assert version.test_cases == ()
    assert [c.evaluator_id for c in version.criteria] == ["rubric_ai_judge"]
    assert sum(c.weight for c in version.criteria) == pytest.approx(1.0)


def test_the_report_distinguishes_auto_graded_from_review_only(
    database: Database, course, tmp_path: Path
) -> None:
    """「取り込めた」と「自動採点できる」は違う。混ぜて報告しない。"""
    shutil.copytree(EXAMPLE_TASK, tmp_path / "ex5" / "p2")
    _server_task(tmp_path)
    report = import_tasks(
        database, course_id=course.id, directory=tmp_path / "ex5", profiles_dir=PROFILES
    )
    assert len(report.imported) == 2
    assert [task.key for task in report.review_only] == ["ex5/p1"]


def test_requiring_test_cases_refuses_a_task_without_them(
    database: Database, course, tmp_path: Path
) -> None:
    """取り込み対象を間違えたことに気づくための安全装置。"""
    report = import_tasks(
        database,
        course_id=course.id,
        directory=_server_task(tmp_path),
        profiles_dir=PROFILES,
        require_test_cases=True,
    )
    assert report.imported == []
    assert "テストケースが 0 件" in report.skipped[0][1]


def test_a_directory_without_problems_is_refused(
    database: Database, course, tmp_path: Path
) -> None:
    with pytest.raises(AdminError, match=r"desc\.md"):
        import_tasks(database, course_id=course.id, directory=tmp_path, profiles_dir=PROFILES)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli(tmp_path: Path, *args: str) -> int:
    return main(
        [
            "--database-url",
            f"sqlite+pysqlite:///{tmp_path}/cli.db",
            "--create-schema",
            "--profiles",
            str(PROFILES),
            *args,
        ]
    )


def test_the_cli_creates_a_course_and_lists_it(tmp_path: Path, capsys) -> None:
    assert (
        _cli(
            tmp_path,
            "course",
            "create",
            "--code",
            "network",
            "--title",
            "ネットワーク及び演習",
            "--term",
            "2025-後期",
            "--profile",
            "cs_intro_c",
        )
        == 0
    )
    course_id = next(
        line.split()[-1] for line in capsys.readouterr().out.splitlines() if "作成" in line
    )

    assert _cli(tmp_path, "course", "list") == 0
    assert course_id in capsys.readouterr().out


def test_the_cli_refuses_to_enrol_without_a_credentials_path(tmp_path: Path, capsys) -> None:
    """パスワードの出力先が無いまま作ると、誰もログインできない利用者が残る。"""
    _cli(
        tmp_path,
        "course",
        "create",
        "--code",
        "network",
        "--title",
        "ネットワーク",
        "--term",
        "2025-後期",
        "--profile",
        "cs_intro_c",
    )
    course_id = next(
        line.split()[-1] for line in capsys.readouterr().out.splitlines() if "作成" in line
    )

    roster = tmp_path / "roster.txt"
    roster.write_text(PLAIN_ROSTER, encoding="utf-8")
    code = _cli(tmp_path, "enrol", "--course", course_id, "--roster", str(roster))
    assert code == 1
    assert "--credentials" in capsys.readouterr().err


def test_the_cli_never_prints_a_password(tmp_path: Path, capsys) -> None:
    """端末の履歴・ログ・画面共有に残さない。"""
    _cli(
        tmp_path,
        "course",
        "create",
        "--code",
        "network",
        "--title",
        "ネットワーク",
        "--term",
        "2025-後期",
        "--profile",
        "cs_intro_c",
    )
    course_id = next(
        line.split()[-1] for line in capsys.readouterr().out.splitlines() if "作成" in line
    )

    roster = tmp_path / "roster.txt"
    roster.write_text(PLAIN_ROSTER, encoding="utf-8")
    creds = tmp_path / "pw.tsv"
    assert (
        _cli(
            tmp_path,
            "enrol",
            "--course",
            course_id,
            "--roster",
            str(roster),
            "--credentials",
            str(creds),
        )
        == 0
    )
    captured = capsys.readouterr()
    passwords = [
        line.split("\t")[1]
        for line in creds.read_text(encoding="utf-8").splitlines()
        if "\t" in line and not line.startswith("#")
    ]
    assert passwords
    for password in passwords:
        assert password not in captured.out
        assert password not in captured.err
