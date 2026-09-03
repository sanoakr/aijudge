"""1 回のログインで両方の画面に入れる（#103）。

**役割はコースに属していて、人には属していない。** 同じ人が「A では学習者・
B では教員」になるのは普通のことなのに、入口が 2 つあると本人はそれに
気づけず、片方しか使えない。

セッションは前から同じ表を共有していた（`AuthService`）。違っていたのは
Cookie の名前だけで、そのために同じ人が同じ機械で 2 回ログインしていた。
ここで固定するのは、**その 1 つの Cookie で両方が開くこと**である。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_admin import ensure_course
from aijudge_core import Role
from aijudge_core.ids import TenantId
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE as CONSOLE_COOKIE
from aijudge_reviewconsole import Console
from aijudge_reviewconsole import create_app as create_console
from aijudge_studentweb import SESSION_COOKIE as LEARNER_COOKIE
from aijudge_studentweb import StudentApp
from aijudge_studentweb import create_app as create_learner
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"


class World:
    """1 つの DB に 2 つのアプリをぶら下げる（本番と同じ形）。"""

    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
        store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.console = TestClient(
            create_console(
                Console(
                    self.database,
                    store,
                    profiles_dir=PROFILES,
                    learner_url="https://learn.example.jp",
                )
            )
        )
        self.learner = TestClient(
            create_learner(
                StudentApp(
                    self.database,
                    store,
                    profiles_dir=PROFILES,
                    console_url="https://teach.example.jp",
                )
            )
        )
        self.taught, _ = ensure_course(
            self.database,
            tenant_id=TENANT,
            code="prog2",
            title="プログラミング及び実習 2",
            term="2026-後期",
            subject_profile="cs_intro_c",
            profiles_dir=PROFILES,
        )
        self.attended, _ = ensure_course(
            self.database,
            tenant_id=TENANT,
            code="stats",
            title="統計学",
            term="2026-後期",
            subject_profile="cs_intro_c",
            profiles_dir=PROFILES,
        )

    def register_dual_role(self, login: str):
        """1 人の人が、片方では教員・もう片方では学習者。"""
        with self.database.unit_of_work() as uow:
            service = AuthService(uow.identity)
            principal = service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
            service.enroll(
                tenant_id=TENANT,
                course_id=self.taught.id,
                user_id=principal.user_id,
                role=Role.INSTRUCTOR,
            )
            service.enroll(
                tenant_id=TENANT,
                course_id=self.attended.id,
                user_id=principal.user_id,
                role=Role.LEARNER,
            )
            uow.commit()
        return principal

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


def test_the_two_apps_use_the_same_cookie(world: World) -> None:
    """**名前が違うだけで 2 回ログインしていた。** 表は前から同じ。"""
    assert CONSOLE_COOKIE == LEARNER_COOKIE


def test_one_login_opens_both(world: World) -> None:
    """片方でログインした Cookie を、もう片方がそのまま受ける。"""
    world.register_dual_role("sano")

    response = world.learner.post(
        "/login", data={"login": "sano", "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    token = response.cookies[LEARNER_COOKIE]

    # **同じ Cookie で教員コンソールが開く。** ログインし直さない。
    world.console.cookies.set(CONSOLE_COOKIE, token)
    landing = world.console.get("/", follow_redirects=False)
    assert landing.status_code == 200
    assert "プログラミング及び実習 2" in landing.text


def test_each_course_opens_the_surface_its_role_allows(world: World) -> None:
    """**役割はコースごと。** 教えるコースと取っているコースが 1 つの一覧に並ぶ。"""
    world.register_dual_role("sano")
    response = world.learner.post(
        "/login", data={"login": "sano", "password": PASSWORD}, follow_redirects=False
    )
    world.console.cookies.set(CONSOLE_COOKIE, response.cookies[LEARNER_COOKIE])

    console_landing = world.console.get("/").text
    # 教えるコースは採点の入口として出る。
    assert f"/courses/{world.taught.id}" in console_landing
    # 取っているコースは「受講しているコース」として、学習者側へ渡す。
    assert "受講しているコース" in console_landing
    assert f"https://learn.example.jp/courses/{world.attended.id}" in console_landing

    learner_landing = world.learner.get("/").text
    # 学習者側の一覧には両方出る。教えるコースには役割と採点への導線が付く。
    assert "統計学" in learner_landing
    assert "プログラミング及び実習 2" in learner_landing
    assert f"https://teach.example.jp/courses/{world.taught.id}" in learner_landing


def test_a_course_the_person_only_attends_has_no_grading_link(world: World) -> None:
    """**取っているだけのコースに採点の導線を出さない。** 押せない口を見せない。"""
    world.register_dual_role("sano")
    response = world.learner.post(
        "/login", data={"login": "sano", "password": PASSWORD}, follow_redirects=False
    )
    world.console.cookies.set(CONSOLE_COOKIE, response.cookies[LEARNER_COOKIE])

    landing = world.console.get("/").text
    assert f"/courses/{world.attended.id}/queue" not in landing
    # 開こうとしても採点はできない（コースそのものが「無い」と答える）。
    assert world.console.get(f"/courses/{world.attended.id}/queue").status_code == 404
