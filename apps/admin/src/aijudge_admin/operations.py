"""管理操作の中身。CLI から切り離してテストできるようにしてある。

**すべて冪等**であることが要点。学期の頭に何度も流し直すもので、
2 回目で「既にある」と落ちたり、パスワードが再生成されて配った紙が
無効になったりしては使えない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aijudge_authoring.importers import sharif_judge
from aijudge_authoring.repository import TaskStoreError
from aijudge_core import Course, Role, Task, TaskVersion
from aijudge_core.ids import CourseId, TenantId, UserId, derived_id, new_id
from aijudge_grading import EvaluatorRegistry, load_profile
from aijudge_identity import AuthenticationFailed, AuthService
from aijudge_persistence import Database

from .roster import RosterEntry, generate_password


class AdminError(Exception):
    """操作を続けられない。"""


# --------------------------------------------------------------------------
# コース
# --------------------------------------------------------------------------


def ensure_course(
    database: Database,
    *,
    tenant_id: TenantId,
    code: str,
    title: str,
    term: str,
    subject_profile: str,
    profiles_dir: Path,
) -> tuple[Course, bool]:
    """コースを用意する。既にあれば題名とプロファイルだけ更新する。

    科目プロファイルの実在を**ここで**確かめる。存在しない名前でコースを
    作れてしまうと、提出は受け付けられるのに採点が恒久的に失敗する
    （ワーカーは `PermanentGradingError` にするしかない）。
    """
    profile_path = profiles_dir / f"{subject_profile}.yaml"
    if not profile_path.is_file():
        raise AdminError(
            f"科目プロファイル {subject_profile!r} がありません（{profile_path}）。"
            "先に subjects/ に置いてください"
        )
    try:
        load_profile(profile_path, EvaluatorRegistry().load_installed())
    except Exception as exc:
        raise AdminError(f"科目プロファイル {subject_profile!r} が不正です: {exc}") from exc

    # コースの同一性は (テナント, 科目コード, 学期)。同じ授業を二度作らない。
    course_id = CourseId(derived_id("crs", str(tenant_id), code, term))
    with database.unit_of_work() as uow:
        existing = uow.identity.get_course(course_id)
        course = Course(
            id=course_id,
            tenant_id=tenant_id,
            code=code,
            title=title,
            term=term,
            subject_profile=subject_profile,
        )
        uow.identity.save_course(course)
        uow.commit()
    return course, existing is None


def list_courses(database: Database, tenant_id: TenantId) -> tuple[Course, ...]:
    from sqlalchemy import select

    from aijudge_persistence.schema import CourseRow

    with database.session() as session:
        rows = session.execute(
            select(CourseRow)
            .where(CourseRow.tenant_id == str(tenant_id))
            .order_by(CourseRow.term, CourseRow.code)
        ).scalars()
        return tuple(
            Course(
                id=CourseId(row.id),
                tenant_id=TenantId(row.tenant_id),
                code=row.code,
                title=row.title,
                term=row.term,
                subject_profile=row.subject_profile,
            )
            for row in rows
        )


# --------------------------------------------------------------------------
# 受講者
# --------------------------------------------------------------------------


@dataclass
class EnrolReport:
    created: list[tuple[str, str]] = field(default_factory=list)
    """(login, password)。**新規に作った利用者だけ。**"""

    enrolled: list[str] = field(default_factory=list)
    """新たにこのコースに登録した login。"""

    already: list[str] = field(default_factory=list)
    """既に利用者があり、受講登録も済んでいた login。"""

    @property
    def total(self) -> int:
        return len(self.created) + len(self.enrolled) + len(self.already)


def enrol_roster(
    database: Database,
    *,
    tenant_id: TenantId,
    course_id: CourseId,
    entries: tuple[RosterEntry, ...],
    dry_run: bool = False,
) -> EnrolReport:
    """名簿の全員を利用者として用意し、コースに登録する。

    **既にある利用者のパスワードは変えない。** 変えると、配ったパスワードが
    名簿を流し直すたびに無効になる。役割の変更（学生 → TA）は反映する。
    """
    report = EnrolReport()

    with database.unit_of_work() as uow:
        if uow.identity.get_course(course_id) is None:
            raise AdminError(f"コース {course_id} がありません。先に course create してください")

        auth = AuthService(uow.identity)
        for entry in entries:
            user = uow.identity.find_user_by_login(tenant_id, entry.login)
            if user is None:
                password = entry.password or generate_password(entry.password_length)
                if not dry_run:
                    principal = auth.register(
                        tenant_id=tenant_id,
                        login=entry.login,
                        display_name=entry.display_name,
                        password=password,
                        email=entry.email,
                    )
                    user_id = principal.user_id
                else:
                    user_id = UserId(new_id("usr"))
                report.created.append((entry.login, password))
            else:
                user_id = user.id
                existing = uow.identity.find_enrollment(course_id, user_id)
                if existing is not None and existing.role is entry.role:
                    report.already.append(entry.login)
                    continue
                report.enrolled.append(entry.login)

            if not dry_run:
                auth.enroll(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    user_id=user_id,
                    role=entry.role,
                )

        if dry_run:
            uow.rollback()
        else:
            uow.commit()
    return report


def set_password(database: Database, *, tenant_id: TenantId, login: str, password: str) -> None:
    """パスワードを再発行する。既存のセッションは切れる。

    平文を保存していないので「思い出す」ことはできない。忘れた学生に対する
    正しい操作はこれ（再発行）である。
    """
    from aijudge_identity import hash_password

    with database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(tenant_id, login)
        if user is None:
            raise AdminError(f"利用者 {login!r} がいません")
        uow.identity.save_user(user.model_copy(update={"password_hash": hash_password(password)}))
        # 乗っ取られていた場合の復旧手段はこれしかない。
        from datetime import UTC, datetime

        uow.identity.revoke_sessions_for(user.id, datetime.now(UTC))
        uow.commit()


def create_staff(
    database: Database,
    *,
    tenant_id: TenantId,
    login: str,
    display_name: str,
    password: str,
    course_id: CourseId | None = None,
    role: Role = Role.INSTRUCTOR,
    email: str | None = None,
) -> bool:
    """教員・TA を作る。既にあれば受講登録だけ行う。"""
    with database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        user = uow.identity.find_user_by_login(tenant_id, login)
        created = user is None
        if user is None:
            try:
                principal = auth.register(
                    tenant_id=tenant_id,
                    login=login,
                    display_name=display_name,
                    password=password,
                    email=email,
                )
            except AuthenticationFailed as exc:
                raise AdminError(str(exc)) from exc
            user_id = principal.user_id
        else:
            user_id = user.id
        if course_id is not None:
            auth.enroll(tenant_id=tenant_id, course_id=course_id, user_id=user_id, role=role)
        uow.commit()
    return created


# --------------------------------------------------------------------------
# 課題
# --------------------------------------------------------------------------


@dataclass
class ImportedTask:
    key: str
    title: str
    test_cases: int
    # 決定的評価器が担当する観点があるか。無ければ AI 観点だけで、
    # 教員の確定が前提になる（サーバ課題・レポート課題・自己採点課題）。
    auto_graded: bool
    # 何回目のまとまりか。一覧の階層化に使う。
    unit: str | None = None
    session: int | None = None


@dataclass
class ImportReport:
    imported: list[ImportedTask] = field(default_factory=list)

    skipped: list[tuple[str, str]] = field(default_factory=list)
    """(課題名, 理由)。取り込めなかったもの。**黙って消さない。**"""

    @property
    def review_only(self) -> list[ImportedTask]:
        """自動テストがまだ無い課題。

        「取り込めなかった」ではなく「取り込めたが AI 観点だけ」。
        運用者がその違いを見落とさないよう、別に数えて出す。
        """
        return [task for task in self.imported if not task.auto_graded]


def import_tasks(
    database: Database,
    *,
    course_id: CourseId,
    directory: Path,
    profiles_dir: Path,
    readability_weight: float = 0.0,
    evaluator_id: str | None = None,
    require_test_cases: bool = False,
    dry_run: bool = False,
) -> ImportReport:
    """Sharif Judge の課題ディレクトリを取り込む。

    `directory` は問題ディレクトリ（`desc.md` を含む）でも、その親
    （`p1/ p2/ p3/` を含む）でも、さらにその親（`ex1/ ex2/ ...`）でもよい。
    運用者が手元にあるディレクトリをそのまま渡せるようにするため。

    **同じ内容の再取り込みは冪等**（課題 ID を課題ディレクトリ名から決定的に
    導くため）。問題文を直した場合だけ「内容が違う」として拒否される。
    その場合は版を上げる操作が別に要る（P8）。

    テストケースが 0 件の課題も取り込む。それは「自動採点できない課題」では
    なく「**まだ**自動採点できない課題」で、実在する（HTTP サーバ課題・
    自己採点課題・レポート課題）。取り込み器がそういう課題を AI 観点だけで
    構成するので、決定的評価器が永久に判定しない観点は生まれない。

    `require_test_cases` を真にすると 0 件の課題を拒否する。取り込み対象を
    間違えた（空のディレクトリを渡した）ことに気づくための安全装置で、
    運用の既定ではない。
    """
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
    if course is None:
        raise AdminError(f"コース {course_id} がありません")

    problem_dirs = _find_problem_dirs(directory)
    if not problem_dirs:
        raise AdminError(f"{directory} に desc.md を持つ問題ディレクトリがありません")

    report = ImportReport()
    for problem_dir in problem_dirs:
        key = f"{problem_dir.parent.name}/{problem_dir.name}"
        try:
            version = sharif_judge.import_problem(
                problem_dir,
                subject_profile=course.subject_profile,
                authored_by=_IMPORTER,
                readability_weight=readability_weight,
                **({"evaluator_id": evaluator_id} if evaluator_id else {}),
            )
        except Exception as exc:
            report.skipped.append((key, f"{type(exc).__name__}: {exc}"))
            continue

        if require_test_cases and not version.test_cases:
            report.skipped.append(
                (key, "テストケースが 0 件（--require-test-cases が指定されている）")
            )
            continue

        unit, session, position = sharif_judge.parse_unit(problem_dir)
        if not dry_run:
            _save(database, course_id, version, key, unit, session, position)
        report.imported.append(
            ImportedTask(
                key=key,
                title=_title_of(version),
                test_cases=len(version.test_cases),
                auto_graded=bool(version.test_cases),
                unit=unit,
                session=session,
            )
        )
    return report


_IMPORTER = UserId(derived_id("usr", "aijudge-admin-importer"))


def _title_of(version: TaskVersion) -> str:
    title, _ = sharif_judge.parse_title(version.statement)
    return title


def _save(
    database: Database,
    course_id: CourseId,
    version: TaskVersion,
    key: str,
    unit: str | None = None,
    session: int | None = None,
    position: int | None = None,
) -> None:
    with database.unit_of_work() as uow:
        existing = uow.tasks.get_task(version.task_id)
        uow.tasks.save_task(
            Task(
                id=version.task_id,
                course_id=course_id,
                title=_title_of(version),
                unit=unit,
                session=session,
                position=position,
                # 取り込み直しで締切を消さない。教員が設定した値を残す。
                opens_at=None if existing is None else existing.opens_at,
                due_at=None if existing is None else existing.due_at,
            )
        )
        try:
            uow.tasks.save_version(version)
        except TaskStoreError as exc:
            raise AdminError(
                f"{key}: 保存済みの課題と内容が違います。問題文を直したなら"
                f"版を上げる必要があります（過去の採点基準は書き換えない、P8）: {exc}"
            ) from exc
        uow.commit()


def _find_problem_dirs(directory: Path) -> tuple[Path, ...]:
    """`desc.md` を持つディレクトリを探す。深さは 2 段まで。

    深く掘らないのは、無関係な `desc.md`（過去年度の控えなど）を
    巻き込まないため。運用者が渡した場所の直下と孫までに留める。
    """
    if not directory.is_dir():
        raise AdminError(f"{directory} がありません")
    if (directory / "desc.md").is_file():
        return (directory,)

    found: list[Path] = []
    for child in sorted(p for p in directory.iterdir() if p.is_dir()):
        if (child / "desc.md").is_file():
            found.append(child)
            continue
        for grandchild in sorted(p for p in child.iterdir() if p.is_dir()):
            if (grandchild / "desc.md").is_file():
                found.append(grandchild)
    return tuple(found)


def list_tasks(database: Database, course_id: CourseId) -> tuple[tuple[Task, TaskVersion], ...]:
    with database.unit_of_work() as uow:
        rows = []
        for task in uow.tasks.list_for_course(course_id):
            version = uow.tasks.latest_version(task.id)
            if version is not None:
                rows.append((task, version))
    return tuple(rows)
