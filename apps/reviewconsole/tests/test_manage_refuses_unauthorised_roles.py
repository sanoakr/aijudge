"""管理画面と API の全経路を、権限の無い利用者で叩いて**拒まれる側**を固定する（#706）。

権限の検査はハンドラごとに 1 行ずつ書かれていて、共有の入口が無い
（`manage/common.py` の `_require_admin`・`_require_instructor`、`access.py`）。
そのため隣の経路のテストは守りにならず、1 行消しても他のテストは通る。
許可される側のテストは揃っているが、拒否が一度も起きていない経路が多かった。

そこで経路の写し（`routes_reviewconsole.txt`）を読み、`/manage` と `/api` の
**全経路**を役割ごとに叩く。経路を足せば自動で対象になり、どの分類にも
入らない経路はそれ自体で落とす（意図して開いている経路は `OPEN_ROUTES` に書く）。

見るのは状態コードだけではない。**DB の全表と、科目プロファイル・提出物の
ファイルが叩く前と同じであること**まで見る ── 403 を返す前に書き込んでいた、
という退行も拾うため。

状態コードは**ちょうど** 403 か 404 を求める。検査が消えると、ハンドラは偽の
値で先へ進んで 303・400・409・422 などを返すので、そこで落ちる。そのために
フォームの必須項目はすべて埋めて送る（埋めないと検査の前に 422 で止まり、
検査があってもなくても同じ応答になる）。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_manage import PROFILES, TENANT, World, _task_with_tests

from aijudge_core import Course, Role
from aijudge_course_admin.operations import ensure_course
from aijudge_identity import AuthService
from aijudge_reviewconsole import Console
from aijudge_submission import FilesystemArtifactStore

ROUTES_SNAPSHOT = Path(__file__).with_name("routes_reviewconsole.txt")

#: テナント全体に効く画面（#128）。**コースの担当教員にも開かない。**
TENANT_ADMIN_PREFIXES = (
    "/manage/users",
    "/manage/subjects",
    "/manage/oidc-settings",
    "/manage/campus-networks",
)
#: コースの作成・削除・複製。コースの外から来る操作なので管理者だけ（#128）。
TENANT_ADMIN_ROUTES = frozenset(
    {
        ("POST", "/manage/courses"),
        ("POST", "/manage/courses/{course_id}/delete"),
        ("POST", "/manage/courses/{course_id}/duplicate"),
    }
)
#: 意図して誰にでも開いている経路。**足すときは理由を書く。**
OPEN_ROUTES = frozenset(
    {
        # 自分の担当コースへの転送だけ。中身は転送先が確かめる。
        ("GET", "/manage"),
        # 自分のパスワードの変更。誰でも自分のものは変えられる。
        ("GET", "/manage/account/password"),
        ("POST", "/manage/account/password"),
        # 空の雛形。コースの中身を含まない。
        ("GET", "/manage/course-template.yaml"),
    }
)
#: TA が**読める**画面（#102）。読むことと直すことは別の権限である。
#: ここに書いたもの以外は、TA にも 403 であることを求める。
ASSISTANT_READABLE = frozenset(
    {
        ("GET", "/manage/courses/{course_id}/tasks/{task_id}/edit"),
        ("GET", "/manage/courses/{course_id}/units/{unit}"),
    }
)
#: 一覧が「足せるコース」だけに絞られている（`api.list_courses`）か、本人を返すだけ。
API_OPEN_ROUTES = frozenset({("GET", "/api/courses"), ("GET", "/api/whoami")})

#: フォームの必須項目（FastAPI の `Form(...)`）を埋める値。**検査の前に 422 で
#: 止まらないように**するためで、中身に意味は無い。余計な項目は無視される。
FORM = {
    "login": "x2400099",
    "role": "instructor",
    "client_id": "client.example",
    "current_password": "wrong password",
    "new_password": "new password 123",
    "new_password_confirm": "new password 123",
    "new_name": "copied_profile",
    "code": "copy9",
    "title": "複製",
    "term_year": "2026",
    "term_division": "前期",
    "profile": "cs_lang_c_intro",
    "instructors": "teacher",
    "specs": "[]",
    "statement": "## 課題 ##\n\n本文",
    "version_id": "tv_" + "0" * 32,
    "roster": "s2400099",
    "key": "cs.loops",
    "key_suffix": "p9",
    "unit": "ex01",
    # 一括確定は根拠の長さを課題の検査より先に見る（`task_ops.finalize_remaining`）。
    "justification": "授業で全員の提出を確認したので確定します。",
}
#: API の本文（JSON）。`TaskSpec`・`MembersRequest`・`AudienceRequest`。
API_BODIES = {
    "/api/courses/{course_id}/tasks": {
        "key": "ex02/p8",
        "statement": "## [必須] 合計 ##\n\n合計を出力する。",
        "unit": "ex02",
        "test_cases": [{"name": "1", "input": "1 2\n", "expected": "3\n"}],
    },
    "/api/courses/{course_id}/groups/{name}": {"members": ["s2400002"]},
    "/api/courses/{course_id}/units/{unit}/audience": {"groups": []},
}
#: 叩くたびに変わってよい表。ログインの記録・トークンの最終利用時刻・拒否そのものの監査記録。
VOLATILE_TABLES = frozenset({"sessions", "api_tokens", "audit_events"})


def _routes() -> list[tuple[str, str]]:
    routes = []
    for line in ROUTES_SNAPSHOT.read_text(encoding="utf-8").splitlines():
        method, path, _handler = line.split(" ")
        if path.startswith(("/manage", "/api")):
            routes.append((method, path))
    return routes


ROUTES = _routes()


def _is_tenant_admin_route(method: str, path: str) -> bool:
    return path.startswith(TENANT_ADMIN_PREFIXES) or (method, path) in TENANT_ADMIN_ROUTES


def _is_course_route(method: str, path: str) -> bool:
    return path.startswith("/manage/courses/{course_id}") and not _is_tenant_admin_route(
        method, path
    )


def _is_api_course_route(method: str, path: str) -> bool:
    return path.startswith("/api/courses/{course_id}")


@dataclass
class Sweep:
    """2 コース・役割ごとの利用者がそろった世界。**モジュールで 1 つ作る。**

    叩く要求はすべて拒まれる前提なので、作り直さずに使い回せる（拒まれずに
    何かが書き込まれたら、その回の DB の比較で落ちる）。
    """

    world: World
    other: Course
    task_id: str
    target_id: str
    profiles: Path
    artifacts: Path
    clients: dict[str, TestClient]
    tokens: dict[str, str]

    def url(self, path: str, *, course_id: str | None = None) -> str:
        values = {
            "course_id": course_id or str(self.world.course.id),
            "task_id": self.task_id,
            "unit": "ex01",
            "user_id": self.target_id,
            "draft_id": "drf_" + "0" * 32,
            # `/manage/subjects/{name}` は科目プロファイル、API はグループの名前。
            "name": "g1" if path.startswith("/api") else "cs_sandbox_kc",
        }
        for key, value in values.items():
            path = path.replace("{" + key + "}", value)
        return path


def _token(world: World, user_id) -> str:
    with world.database.unit_of_work() as uow:
        _record, token = AuthService(uow.identity, audit=uow.audit).issue_token(
            tenant_id=TENANT, user_id=user_id, note="#706 の拒否の確認"
        )
        uow.commit()
    return token


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sweep]:
    root = tmp_path_factory.mktemp("sweep")
    world = World(root)
    # **科目プロファイルは写しを使う。** 保存の検査が消えた退行では、リポジトリの
    # `subjects/` が書き換わってしまう。
    profiles = root / "profiles"
    shutil.copytree(PROFILES, profiles)
    artifacts = root / "artifacts"
    world.console = Console(
        world.database, FilesystemArtifactStore(artifacts), profiles_dir=profiles
    )
    other, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="net",
        title="ネットワーク及び演習",
        term="2025-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    teacher = world.register("teacher", Role.INSTRUCTOR)
    ta = world.register("ta", Role.ASSISTANT)
    student = world.register("student", Role.LEARNER)
    target = world.register("s2400002", Role.LEARNER)
    # 別のコースの担当教員。こちらのコースには何の受講も無い。
    outsider = world.register("outsider", Role.INSTRUCTOR, other.id)
    task_id = _task_with_tests(world)
    instance = Sweep(
        world=world,
        other=other,
        task_id=task_id,
        target_id=str(target.user_id),
        profiles=profiles,
        artifacts=artifacts,
        clients={login: world.client(login) for login in ("teacher", "ta", "student", "outsider")},
        tokens={
            login: _token(world, principal.user_id)
            for login, principal in (
                ("teacher", teacher),
                ("ta", ta),
                ("student", student),
                ("outsider", outsider),
            )
        },
    )
    yield instance
    world.close()


def _state(sweep: Sweep) -> dict[str, list[str]]:
    """DB の全表（変わってよい表を除く）と、プロファイル・提出物のファイル。"""
    metadata = sa.MetaData()
    engine = sweep.world.database.engine
    metadata.reflect(engine)
    state: dict[str, list[str]] = {}
    with engine.connect() as connection:
        for name, table in sorted(metadata.tables.items()):
            if name in VOLATILE_TABLES:
                continue
            state[name] = sorted(repr(tuple(row)) for row in connection.execute(table.select()))
    for label, directory in (("profiles", sweep.profiles), ("artifacts", sweep.artifacts)):
        state[f"files:{label}"] = sorted(
            f"{path.relative_to(directory)}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            for path in directory.rglob("*")
            if path.is_file()
        )
    return state


def _send(client: TestClient, method: str, url: str, path: str, *, headers=None):
    if path.startswith("/api"):
        body = API_BODIES.get(path)
        return client.request(method, url, json=body, headers=headers, follow_redirects=False)
    if method == "GET":
        return client.get(url, follow_redirects=False)
    files = None
    if path.endswith("/images.json"):
        files = {"upload": ("figure.png", b"\x89PNG\r\n\x1a\n", "image/png")}
    return client.request(method, url, data=FORM, files=files, follow_redirects=False)


def _assert_refused(sweep: Sweep, login: str, method: str, path: str, expected: int, **kw) -> None:
    before = _state(sweep)
    url = sweep.url(path, course_id=kw.get("course_id"))
    headers = None
    client = sweep.clients[login]
    if path.startswith("/api"):
        # API は Cookie では通らない（`test_api.py`）ので、トークンで叩く。
        headers = {"Authorization": f"Bearer {sweep.tokens[login]}"}
    response = _send(client, method, url, path, headers=headers)
    assert response.status_code == expected, (
        f"{login} {method} {url} → {response.status_code}: {response.text[:300]}"
    )
    assert _state(sweep) == before, f"{login} {method} {url} が拒まれる前に何かを書き換えた"


# --------------------------------------------------------------------------
# 分類の漏れ
# --------------------------------------------------------------------------


def test_every_manage_and_api_route_is_classified() -> None:
    """**どの分類にも入らない経路を作らせない。** 足された経路は、どれかに入れる
    まで（＝拒まれる側を決めるまで）ここで落ちる。"""
    unclassified = [
        f"{method} {path}"
        for method, path in ROUTES
        if (method, path) not in OPEN_ROUTES | API_OPEN_ROUTES
        and not _is_tenant_admin_route(method, path)
        and not _is_course_route(method, path)
        and not _is_api_course_route(method, path)
    ]
    assert unclassified == []
    # 許可リストに、もう無い経路を残さない（残すと次に足された経路を黙って通す）。
    known = set(ROUTES)
    stale = (OPEN_ROUTES | API_OPEN_ROUTES | ASSISTANT_READABLE | TENANT_ADMIN_ROUTES) - known
    assert stale == set()


# --------------------------------------------------------------------------
# テナント全体の画面 … 管理者だけ
# --------------------------------------------------------------------------

TENANT_ADMIN = [(m, p) for m, p in ROUTES if _is_tenant_admin_route(m, p)]


@pytest.mark.parametrize(
    ("method", "path"), TENANT_ADMIN, ids=[f"{m} {p}" for m, p in TENANT_ADMIN]
)
@pytest.mark.parametrize("login", ["teacher", "ta", "student", "outsider"])
def test_a_non_admin_is_refused_on_tenant_wide_routes(
    sweep: Sweep, login: str, method: str, path: str
) -> None:
    """管理者フラグ・パスワードの再発行・OIDC・学内範囲・科目プロファイルは
    テナント全体に効く。**そのコースの担当教員でも 403**（#128）。

    `teacher` はこの世界のコースの担当教員で、`/manage/users/{user_id}/courses/
    {course_id}/role` の `course_id` も自分のコースを指す ── それでも開かない。
    """
    _assert_refused(sweep, login, method, path, 403)


# --------------------------------------------------------------------------
# コース単位の画面 … 担当教員だけ（TA には開けない、#102）
# --------------------------------------------------------------------------

COURSE = [(m, p) for m, p in ROUTES if _is_course_route(m, p)]
COURSE_FOR_ASSISTANT = [(m, p) for m, p in COURSE if (m, p) not in ASSISTANT_READABLE]


@pytest.mark.parametrize(
    ("method", "path"), COURSE_FOR_ASSISTANT, ids=[f"{m} {p}" for m, p in COURSE_FOR_ASSISTANT]
)
def test_an_assistant_is_refused_on_course_routes(sweep: Sweep, method: str, path: str) -> None:
    """**TA は採点を分担するが、締切・配点・テストケース・受講は変えられない**（#102）。

    受講者の役割の変更（`enrolments/{user_id}/role`）は、TA が他の TA や学習者を
    instructor に上げる経路になる ── 送るフォームは `role=instructor` にしてある。
    受講の取り消し・学習者ごとの習熟度も同じく 403。
    """
    _assert_refused(sweep, "ta", method, path, 403)


@pytest.mark.parametrize(("method", "path"), COURSE, ids=[f"{m} {p}" for m, p in COURSE])
def test_a_learner_is_refused_on_course_routes(sweep: Sweep, method: str, path: str) -> None:
    """学習者は自分のコースでも管理画面には入れない。TA が読める画面も含めて 403。"""
    _assert_refused(sweep, "student", method, path, 403)


@pytest.mark.parametrize(("method", "path"), COURSE, ids=[f"{m} {p}" for m, p in COURSE])
def test_another_courses_instructor_gets_404_on_course_routes(
    sweep: Sweep, method: str, path: str
) -> None:
    """受講の無いコースは「無い」と答える（存在と権限を区別しない。`access.py`）。"""
    _assert_refused(sweep, "outsider", method, path, 404)


# --------------------------------------------------------------------------
# URL のコースと課題が食い違う … 他コースの課題に届かない
# --------------------------------------------------------------------------

TASK_ROUTES = [(m, p) for m, p in COURSE if "{task_id}" in p]


@pytest.mark.parametrize(("method", "path"), TASK_ROUTES, ids=[f"{m} {p}" for m, p in TASK_ROUTES])
def test_a_task_of_another_course_is_not_reachable_through_ones_own_course(
    sweep: Sweep, method: str, path: str
) -> None:
    """**URL の `course_id` は自分のコース、`task_id` は他コースの課題。**

    コースの検査（担当教員か）は自分のコースに対して通るので、止めるのは
    「課題がそのコースのものか」の検査だけ（`task_page._task_of` と、各ハンドラの
    `task.course_id != CourseId(course_id)`）。これが消えると、どのコースの教員でも
    他コースの課題を削除・書き換え・公開前に読めてしまう。
    """
    _assert_refused(sweep, "outsider", method, path, 404, course_id=str(sweep.other.id))


# --------------------------------------------------------------------------
# API … トークンでも同じ規則
# --------------------------------------------------------------------------

API = [(m, p) for m, p in ROUTES if _is_api_course_route(m, p)]


@pytest.mark.parametrize(("method", "path"), API, ids=[f"{m} {p}" for m, p in API])
@pytest.mark.parametrize(("login", "expected"), [("ta", 403), ("student", 403), ("outsider", 404)])
def test_the_api_refuses_by_the_same_rules(
    sweep: Sweep, login: str, expected: int, method: str, path: str
) -> None:
    """API は画面と同じ `access.require_instructor` を通る。トークンは利用者の
    権限で動くので、TA・学習者のトークンは 403、受講の無いコースは 404。"""
    _assert_refused(sweep, login, method, path, expected)


def test_the_sweep_is_not_vacuous(sweep: Sweep) -> None:
    """**同じ要求が担当教員なら検査を越える**ことを、読むだけの経路で確かめる。

    越えないなら、上の 403/404 は検査ではなく世界の作り方（課題が無い・URL が
    違う）から来ている。書き込みの経路は叩かない ── 世界を共有しているため。
    """
    teacher = sweep.clients["teacher"]
    for path in (
        "/manage/courses/{course_id}/tasks/{task_id}/edit",
        "/manage/courses/{course_id}/units/{unit}",
        "/manage/courses/{course_id}/enrolments",
        "/manage/courses/{course_id}/mastery/{user_id}",
    ):
        assert teacher.get(sweep.url(path)).status_code == 200, path
    headers = {"Authorization": f"Bearer {sweep.tokens['teacher']}"}
    listed = teacher.get(sweep.url("/api/courses/{course_id}/tasks"), headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [sweep.task_id]
