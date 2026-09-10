"""骨格が体系を縛ることを固定する。

固定したいのは 4 つ。

骨格が決める  分野（第 1 階層）と単位（第 2 階層）は `subjects/kc/*.yaml` だけ。
              画面からも AI からも足せない ── 開けると `cs.loops` と
              `cs.iteration` が並ぶ。
深さは 3      知識要素は第 3 階層まで。細かくしすぎると 1 つの KC に課題が
              1 件しか対応せず、習熟度が推定できない。
足せる        知識要素は教員が足せる。骨格は推奨候補であって正解の一覧ではない。
近いものを出す 足すときは近い既存 KC を見せる。**分野と単位をまたいで探す** ──
              別の枝にある同義語こそ、目では見つからない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin import (
    AdminError,
    list_for_namespaces,
    load_skeleton,
    register_kc,
    seed_kcs,
    suggest_similar,
)
from aijudge_core.ids import UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
SKELETON = REPO_ROOT / "subjects" / "kc" / "cs.yaml"
MATH_SKELETON = REPO_ROOT / "subjects" / "kc" / "math.yaml"
TEACHER = UserId("usr_" + "1" * 32)
SPACES = ("cs",)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/kc.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def seeded(database: Database) -> Database:
    seed_kcs(database, load_skeleton(SKELETON), namespaces=SPACES)
    return database


# --------------------------------------------------------------------------
# 投入
# --------------------------------------------------------------------------


def test_the_skeleton_in_the_repository_loads(database: Database) -> None:
    """**実物を読む。** 作り物の骨格で通しても、配ったファイルが壊れていたら
    運用は動かない。"""
    skeleton = load_skeleton(SKELETON)
    assert skeleton.namespace == "cs"
    assert skeleton.source
    # CS2023 の 17 分野。
    assert len(skeleton.areas) == 17
    assert len(skeleton.units) > 100
    assert len(skeleton.components) > 500
    keys = {e.key for e in skeleton.entries}
    assert "sdf" in keys
    assert "sdf.fundamentals" in keys
    assert "sdf.fundamentals.branching" in keys


def test_seeding_twice_does_not_duplicate(database: Database) -> None:
    """**何度走らせても増えない。** 骨格を直して足したら、また走らせるだけ。"""
    skeleton = load_skeleton(SKELETON)
    first = seed_kcs(database, skeleton, namespaces=SPACES)
    assert first.added and not first.existing

    second = seed_kcs(database, skeleton, namespaces=SPACES)
    assert not second.added
    assert len(second.existing) == first.total
    assert len(list_for_namespaces(database, SPACES)) == first.total


def test_the_report_separates_new_from_existing(database: Database) -> None:
    """「17 件投入しました」だけだと、2 度目の運用者に何が起きたか伝わらない。"""
    report = seed_kcs(database, load_skeleton(SKELETON), namespaces=SPACES)
    assert "新規" in report.summary()
    assert report.source in report.summary()


def test_a_namespace_the_profile_did_not_declare_is_refused(database: Database) -> None:
    with pytest.raises(AdminError, match="名前空間"):
        seed_kcs(database, load_skeleton(SKELETON), namespaces=("writing",))


def test_a_broken_skeleton_is_not_partly_applied(database: Database, tmp_path: Path) -> None:
    """**黙って半分だけ入れない。** 木が半分ある状態は、教員には
    「なぜかこの単位だけ無い」としか見えない。"""
    broken = tmp_path / "broken.yaml"
    broken.write_text("namespace: cs\nareas:\n  - label: キーがない\n", encoding="utf-8")
    with pytest.raises(AdminError, match="key"):
        load_skeleton(broken)
    assert list_for_namespaces(database, SPACES) == ()


def test_a_duplicate_key_in_the_skeleton_is_caught_before_seeding(
    database: Database, tmp_path: Path
) -> None:
    """投入してからでは直せない（ID がキーから決まるので片方だけ消せない）。"""
    dup = tmp_path / "dup.yaml"
    dup.write_text(
        "namespace: cs\n"
        "areas:\n"
        "  - key: sdf\n"
        "    label: 基礎\n"
        "    units:\n"
        "      - key: fundamentals\n"
        "        label: 基本\n"
        "        components:\n"
        "          - {key: branching, label: 分岐}\n"
        "          - {key: branching, label: 条件分岐}\n",
        encoding="utf-8",
    )
    with pytest.raises(AdminError, match="重複"):
        load_skeleton(dup)


# --------------------------------------------------------------------------
# 骨格が縛る
# --------------------------------------------------------------------------


def test_a_teacher_cannot_add_an_area(seeded: Database) -> None:
    with pytest.raises(AdminError, match="骨格"):
        register_kc(seeded, key="cs.newfield", label="新分野", namespaces=SPACES, actor_id=TEACHER)


def test_a_teacher_cannot_add_a_unit(seeded: Database) -> None:
    """ここを開けると `cs.sdf.loops` と `cs.sdf.iteration` が並ぶ。"""
    with pytest.raises(AdminError, match="骨格"):
        register_kc(
            seeded, key="cs.sdf.newunit", label="新単位", namespaces=SPACES, actor_id=TEACHER
        )


def test_the_depth_stops_at_three(seeded: Database) -> None:
    """**細かくしすぎない。** 1 つの KC に課題が 1 件しか対応しないと、
    習熟度が推定できない（Q-matrix が薄くなる）。"""
    with pytest.raises(AdminError, match="第 3 階層"):
        register_kc(
            seeded,
            key="cs.sdf.fundamentals.branching.nested",
            label="入れ子の分岐",
            namespaces=SPACES,
            actor_id=TEACHER,
        )


def test_a_teacher_can_add_a_component(seeded: Database) -> None:
    """骨格は推奨候補であって正解の一覧ではない。科目の専門家は教員しかいない。"""
    kc = register_kc(
        seeded,
        key="cs.sdf.fundamentals.pointer_basics",
        label="ポインタの基礎",
        namespaces=SPACES,
        actor_id=TEACHER,
    )
    assert kc.key == "cs.sdf.fundamentals.pointer_basics"
    assert kc.parent_key == "cs.sdf.fundamentals"
    # **誰が足したかを残す**（骨格は created_by なし、教員のものは付く）。
    assert kc.created_by == TEACHER


def test_the_skeleton_itself_is_not_attributed_to_anyone(seeded: Database) -> None:
    """骨格は誰か個人が足したものではなく、ファイルをレビューして決めたもの。"""
    kc = next(k for k in list_for_namespaces(seeded, SPACES) if k.key == "cs.sdf.fundamentals")
    assert kc.created_by is None


def test_an_unknown_unit_does_not_tell_the_teacher_to_add_it(seeded: Database) -> None:
    """**「先に追加してください」と言わない。** 足せないものを足そうとして
    二度断られることになる。"""
    with pytest.raises(AdminError) as exc:
        register_kc(
            seeded, key="cs.sdf.nosuch.thing", label="試し", namespaces=SPACES, actor_id=TEACHER
        )
    assert "骨格" in str(exc.value)
    assert "先に" not in str(exc.value)


# --------------------------------------------------------------------------
# 近いものを見せる
# --------------------------------------------------------------------------


def test_a_near_duplicate_in_the_same_unit_is_offered(seeded: Database) -> None:
    hits = suggest_similar(
        seeded, key="cs.nc.applications.socket", label="ソケット", namespaces=SPACES
    )
    assert any(h.key == "cs.nc.applications.socket_api" for h in hits)
    assert all(h.same_unit for h in hits if h.key == "cs.nc.applications.socket_api")


def test_a_near_duplicate_in_another_branch_is_offered(seeded: Database) -> None:
    """**別の枝こそ重要。** 同じ枝の重複は目で見つかるが、別の分野に同じ語が
    あることは一覧を見ても気づけない ── これがご懸念の「同じ知識が異なる
    分野に散在する」経路である。"""
    hits = suggest_similar(
        seeded, key="cs.sdf.data_structures.arrays", label="配列", namespaces=SPACES
    )
    keys = {h.key for h in hits}
    assert "cs.sdf.data_structures.array" in keys
    assert "cs.al.foundational.array" in keys
    assert any(not h.same_unit for h in hits)


def test_a_shorter_label_inside_a_longer_one_is_found(seeded: Database) -> None:
    """「くりかえし」は「回数の決まったくりかえし」を見つけなければならない。

    Jaccard だけだと和集合が大きくなって値が伸びず、見落とす。実際に見落とした。
    """
    hits = suggest_similar(
        seeded, key="cs.sdf.fundamentals.loop", label="くりかえし", namespaces=SPACES
    )
    keys = {h.key for h in hits}
    assert "cs.sdf.fundamentals.definite_loop" in keys
    assert "cs.sdf.fundamentals.indefinite_loop" in keys


def test_english_keys_do_not_match_by_accident(seeded: Database) -> None:
    """短い英語の識別子は 3-gram を偶然共有する。

    `loop` を `event_loop`・`game_loop` にも一致させると、肝心の候補が
    押し出される。**日本語のラベルと英語のキーを混ぜて測らない。**
    """
    hits = suggest_similar(
        seeded, key="cs.sdf.fundamentals.loop", label="くりかえし", namespaces=SPACES
    )
    keys = {h.key for h in hits}
    assert "cs.fpl.event_driven.event_loop" not in keys
    assert "cs.spd.interactive.game_loop" not in keys


def test_something_genuinely_new_gets_no_suggestions(seeded: Database) -> None:
    """近いものが無いときに何か出すと、提示そのものが信用されなくなる。"""
    hits = suggest_similar(
        seeded,
        key="cs.sdf.fundamentals.interstellar_signalling",
        label="宇宙人との交信",
        namespaces=SPACES,
    )
    assert hits == ()


def test_areas_and_units_are_never_suggested(seeded: Database) -> None:
    """足せるのは知識要素だけなので、単位に「寄せては」と言われても寄せられない。"""
    hits = suggest_similar(
        seeded, key="cs.sdf.fundamentals.fundamental", label="基本", namespaces=SPACES
    )
    assert all(len(h.kc.path) == 3 for h in hits)


# --------------------------------------------------------------------------
# 数学の骨格（#187）
# --------------------------------------------------------------------------


def test_the_math_skeleton_in_the_repository_loads(database: Database) -> None:
    """cs.yaml と同じく**実物を読む。**"""
    skeleton = load_skeleton(MATH_SKELETON)
    assert skeleton.namespace == "math"
    assert skeleton.source
    # CUPM 2015 の Course Area Study Group 19 分野 + precalculus。
    assert len(skeleton.areas) == 20
    assert len(skeleton.units) > 100
    assert len(skeleton.components) > 500
    keys = {e.key for e in skeleton.entries}
    assert "linear_algebra" in keys
    assert "linear_algebra.eigenvalues" in keys
    assert "linear_algebra.eigenvalues.diagonalization" in keys


def test_the_math_skeleton_does_not_branch_on_school_subjects() -> None:
    """**第 1 階層に「数学Ⅰ」「数学Ｃ」を作らない。**

    作ると KC の同一性が告示の版に張り付く ── 行列は「代数・幾何」→
    「数学Ｃ」→ 削除 →「数学Ｃ」と動き、複素数平面は「数学Ｂ」→「数学Ⅲ」→
    「数学Ｃ」と動いた。`math.math_c.matrix.rank` は次の改訂で行き場を失うが、
    ID は追記のみ（P8）なので消せない。分野は数学の内容で切る。
    """
    areas = {e.key for e in load_skeleton(MATH_SKELETON).areas}
    forbidden = {f"math_{s}" for s in ("i", "ii", "iii", "a", "b", "c", "1", "2", "3")}
    assert not areas & forbidden


def test_high_school_and_university_share_one_namespace(database: Database) -> None:
    """高校の三角関数と大学の微分積分が**同じ語彙に載る。**

    分けると、大学 1 年でつまずいた学生の弱点が高校の課題と同じ KC に
    落ちなくなる（名前空間をまたいだ親子関係は持たない ── P6）。
    """
    seed_kcs(database, load_skeleton(MATH_SKELETON), namespaces=("math",))
    keys = {kc.key for kc in list_for_namespaces(database, ("math",))}
    assert "math.precalculus.trigonometric_function.addition_theorem" in keys
    assert "math.calculus.differentiation.chain_rule" in keys


def test_math_and_cs_do_not_collide(database: Database) -> None:
    """同じ DB に両方入れても混ざらない ── 名前空間で分かれている。"""
    seed_kcs(database, load_skeleton(SKELETON), namespaces=SPACES)
    seed_kcs(database, load_skeleton(MATH_SKELETON), namespaces=("math",))
    cs_keys = {kc.key for kc in list_for_namespaces(database, SPACES)}
    math_keys = {kc.key for kc in list_for_namespaces(database, ("math",))}
    assert cs_keys and math_keys
    assert not cs_keys & math_keys
