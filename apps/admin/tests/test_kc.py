"""知識要素の体系を保つ規則を固定する。

固定したいのは 4 つ（`aijudge_admin.kc` の docstring と対）。

名前空間  科目プロファイルが宣言したものだけ。教員は増やせない。
親が要る  新しい KC は既存 KC の子としてのみ足せる。孤立キーを作らせない。
消さない  誤りは引退させて後継を指す。ID がキーから導かれ、Q-matrix は
          追記のみなので、消すと過去の課題が何を問うていたか辿れない。
登録必須  課題は登録済みの KC しか名指しできない。綴り違いが静かに
          新しい KC を作ると、Q-matrix が同じものを 2 つに割る。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin import (
    AdminError,
    assert_registered,
    ensure_course,
    kc_usage,
    list_for_namespaces,
    register_kc,
    restore_kc,
    retire_kc,
    save_task,
)
from aijudge_authoring import TaskSpec
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)
SPACES = ("cs",)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/kc.db", create=True)
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


def _root(database: Database):
    return register_kc(database, key="cs.loops", label="ループ", namespaces=SPACES, allow_root=True)


# --------------------------------------------------------------------------
# 名前空間
# --------------------------------------------------------------------------


def test_a_namespace_the_profile_did_not_declare_is_refused(database: Database) -> None:
    """ブラウザから名前空間を作れると、`cs` と `csci` の分裂が起きる。"""
    with pytest.raises(AdminError) as exc:
        register_kc(database, key="csci.loops", label="ループ", namespaces=SPACES, allow_root=True)
    assert "名前空間" in str(exc.value)


def test_the_declared_namespace_is_accepted(database: Database) -> None:
    kc = _root(database)
    assert kc.key == "cs.loops"
    assert kc.namespace == "cs"


# --------------------------------------------------------------------------
# 親が実在すること
# --------------------------------------------------------------------------


def test_a_child_needs_its_parent(database: Database) -> None:
    """孤立キーの山ではなく木を保つ。"""
    with pytest.raises(AdminError) as exc:
        register_kc(database, key="cs.loops.termination", label="停止条件", namespaces=SPACES)
    assert "親" in str(exc.value)


def test_a_child_is_accepted_once_the_parent_exists(database: Database) -> None:
    parent = _root(database)
    child = register_kc(database, key="cs.loops.termination", label="停止条件", namespaces=SPACES)
    assert child.parent_id == parent.id
    assert child.parent_key == "cs.loops"


def test_a_root_needs_an_administrator(database: Database) -> None:
    """新しい分野の根を作るのは稀で意図的な操作。"""
    with pytest.raises(AdminError) as exc:
        register_kc(database, key="cs.recursion", label="再帰", namespaces=SPACES)
    assert "第 1 階層" in str(exc.value)


def test_registering_the_same_key_twice_does_not_duplicate(database: Database) -> None:
    first = _root(database)
    second = register_kc(
        database, key="cs.loops", label="別の名前", namespaces=SPACES, allow_root=True
    )
    assert first.id == second.id
    assert len(list_for_namespaces(database, SPACES)) == 1


def test_a_malformed_key_is_refused(database: Database) -> None:
    for key in ("loops", "cs..loops", "CS.loops", "cs.1loops"):
        with pytest.raises(AdminError):
            register_kc(database, key=key, label="x", namespaces=SPACES, allow_root=True)


# --------------------------------------------------------------------------
# 引退（改名はできない）
# --------------------------------------------------------------------------


def test_retiring_keeps_the_component(database: Database) -> None:
    """消さない。消すと過去の課題が何を問うていたのか辿れなくなる（P8）。"""
    _root(database)
    register_kc(database, key="cs.loops.termination", label="停止条件", namespaces=SPACES)
    register_kc(database, key="cs.loops.terminaton", label="打ち間違い", namespaces=SPACES)

    retired = retire_kc(
        database, key="cs.loops.terminaton", superseded_by_key="cs.loops.termination"
    )
    assert retired.deprecated
    assert retired.superseded_by is not None
    # 一覧からは消えない。引退として残る。
    keys = [kc.key for kc in list_for_namespaces(database, SPACES)]
    assert "cs.loops.terminaton" in keys
    # 現役だけを求めれば出てこない。
    live = [kc.key for kc in list_for_namespaces(database, SPACES, include_deprecated=False)]
    assert "cs.loops.terminaton" not in live


def test_a_retired_component_cannot_be_a_successor(database: Database) -> None:
    """辿った先がまた引退している、を作らない。"""
    _root(database)
    register_kc(database, key="cs.loops.a", label="a", namespaces=SPACES)
    register_kc(database, key="cs.loops.b", label="b", namespaces=SPACES)
    retire_kc(database, key="cs.loops.b")
    with pytest.raises(AdminError):
        retire_kc(database, key="cs.loops.a", superseded_by_key="cs.loops.b")


def test_retirement_can_be_undone(database: Database) -> None:
    _root(database)
    retire_kc(database, key="cs.loops")
    revived = restore_kc(database, key="cs.loops")
    assert not revived.deprecated
    assert revived.superseded_by is None


# --------------------------------------------------------------------------
# 登録済みの KC しか課題に付けられない
# --------------------------------------------------------------------------


def test_an_unregistered_component_is_refused(database: Database) -> None:
    with pytest.raises(AdminError) as exc:
        assert_registered(database, ("cs.loops.termination",))
    assert "登録されていない" in str(exc.value)


def test_a_task_cannot_name_an_unregistered_component(database: Database, course) -> None:
    """綴り違いが静かに新しい KC を作ると、Q-matrix が同じものを 2 つに割る。"""
    spec = TaskSpec(
        key="ex01/p1",
        statement="## 課題 ##\n\n本文",
        knowledge_components=("cs.loops.terminaton",),
    )
    with pytest.raises(AdminError):
        save_task(
            database,
            course_id=course.id,
            spec=spec,
            subject_profile="cs_intro_c",
            authored_by=TEACHER,
        )


def test_a_task_with_registered_components_is_saved(database: Database, course) -> None:
    _root(database)
    register_kc(database, key="cs.loops.termination", label="停止条件", namespaces=SPACES)
    spec = TaskSpec(
        key="ex01/p1",
        statement="## 課題 ##\n\n本文",
        knowledge_components=("cs.loops.termination",),
    )
    saved = save_task(
        database,
        course_id=course.id,
        spec=spec,
        subject_profile="cs_intro_c",
        authored_by=TEACHER,
    )
    assert saved.version.q_matrix


# --------------------------------------------------------------------------
# 利用状況（コースをまたいで数える）
# --------------------------------------------------------------------------


def test_usage_counts_tasks_and_courses(database: Database, course) -> None:
    """自分のコースで使っていなくても、他のコースが使っていれば影響がある。"""
    _root(database)
    register_kc(database, key="cs.loops.termination", label="停止条件", namespaces=SPACES)
    save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key="ex01/p1",
            statement="## 課題 ##\n\n本文",
            knowledge_components=("cs.loops.termination",),
        ),
        subject_profile="cs_intro_c",
        authored_by=TEACHER,
    )

    rows = kc_usage(database, list_for_namespaces(database, SPACES))
    assert rows["cs.loops.termination"].tasks == 1
    assert rows["cs.loops.termination"].courses == 1
    assert rows["cs.loops"].tasks == 0
