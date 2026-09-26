"""コースの一覧は、インメモリ実装と SQL 実装で同じに振る舞う（段階的な立て直し 1-3）。

固定したいのは 3 つ。

範囲    `list_courses` はテナントで絞り、`list_all_courses` は絞らない
        （自動確定と KC の利用状況は、テナントを足した日に漏れてはならない）。
並び    (学期, コード)。学期は時系列（#167）で、文字列の順ではない。
中身    一覧から読んだ `Course` は `get_course` と同じ。以前 `aijudge_admin` が
        `CourseRow` から手で組み立てていた写しは、遅延の減点・KC・ルーブリックの
        畳み方を読み落としていた。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest

from aijudge_core import Aggregation, Course, LatePenaltyStep
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import InMemoryIdentityRepository
from aijudge_persistence import Database

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")
TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)


def _course(n: int, *, tenant: TenantId, code: str, term: str) -> Course:
    return Course(
        id=CourseId("crs_" + str(n) * 32),
        tenant_id=tenant,
        code=code,
        title=code,
        term=term,
        subject_profile="cs_lang_c_intro",
    )


# 学期の文字列順（"2026-前期" < "2026-後期" < "2027-前期" とは限らない）と
# 時系列が食い違う組を混ぜる。
COURSES = (
    _course(1, tenant=TENANT, code="prog2", term="2026-後期"),
    _course(2, tenant=TENANT, code="prog1", term="2026-前期"),
    _course(3, tenant=OTHER_TENANT, code="network", term="2026-前期"),
    _course(4, tenant=TENANT, code="algo", term="2026-後期"),
)
FULL = Course(
    id=CourseId("crs_" + "5" * 32),
    tenant_id=OTHER_TENANT,
    code="report",
    title="レポート",
    term="2027-前期",
    subject_profile="cs_lang_c_intro",
    description="概要",
    rubric=({"id": "c1", "weight": 1.0},),
    rubric_aggregation=Aggregation.AND,
    knowledge_components=("prog.loop",),
    grading_overrides={"k": 1},
    auto_finalize_after_minutes=60,
    upload_suffixes=(".pdf",),
    late_penalty_steps=(LatePenaltyStep(after_hours=24.0, ratio=0.1),),
)


@pytest.fixture(params=["memory", "sqlite"] + (["postgres"] if POSTGRES_URL else []))
def identity(request) -> Iterator[Callable]:
    """1 操作ごとに保存先を開いて閉じる。SQL では 1 操作 = 1 トランザクション。"""
    if request.param == "memory":
        memory = InMemoryIdentityRepository()
        run: Callable = lambda action: action(memory)  # noqa: E731
        _seed(run)
        yield run
        return

    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    database = Database.connect(url, create=False)
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)

    def run_sql(action):
        with database.unit_of_work() as uow:
            result = action(uow.identity)
            uow.commit()
        return result

    _seed(run_sql)
    yield run_sql
    database.dispose()


def _seed(run: Callable) -> None:
    def seed(identity) -> None:
        for course in (*COURSES, FULL):
            identity.save_course(course)

    run(seed)


def test_listing_a_tenant_stays_in_the_tenant(identity) -> None:
    codes = [c.code for c in identity(lambda repo: repo.list_courses(TENANT))]
    assert codes == ["prog1", "algo", "prog2"]


def test_listing_everything_crosses_tenants_in_term_order(identity) -> None:
    codes = [c.code for c in identity(lambda repo: repo.list_all_courses())]
    assert codes == ["network", "prog1", "algo", "prog2", "report"]


def test_a_listed_course_is_the_whole_course(identity) -> None:
    """一覧からも、1 件を引いたときと同じ `Course` が返る（列を読み落とさない）。"""
    listed = {c.id: c for c in identity(lambda repo: repo.list_all_courses())}
    assert listed[FULL.id] == FULL
    assert listed[FULL.id] == identity(lambda repo: repo.get_course(FULL.id))
