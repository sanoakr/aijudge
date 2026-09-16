"""学習者に知識要素を出す（#327 の第 1 段階）。

固定したいのは 4 つ。

事実だけ出す   出すのは Q-matrix そのもの。**習熟度（推定）は出さない。**
語彙で出す     鍵ではなく表示名（`cs.sdf...` は学習者には読めない）。
無い課題は黙る 知識要素の無い課題では節ごと出ない（「なし」とも書かない）。
引けないものは出さない 語彙に無い KC を鍵のまま並べない。
"""

from __future__ import annotations

from test_studentweb import World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import new_id
from aijudge_core.ids import KcId, TaskVersionId
from aijudge_core.knowledge import KnowledgeComponent, QMatrixEntry

LOOPS = KcId("kc_" + "a" * 32)
ARRAY = KcId("kc_" + "b" * 32)
MISSING = KcId("kc_" + "c" * 32)


def _teach(world: World, *kc_ids: KcId) -> str:
    """知識要素を付けた**新しい版**を作り、その ID を返す。

    既にある版は書き換えない ── 公開後の版は不変で、過去の採点がどの基準で
    付いたのかを辿れなくなる（P8・`TaskImmutabilityViolation`）。
    """
    with world.database.unit_of_work() as uow:
        for kc_id, path, label in (
            (LOOPS, ("sdf", "fundamentals", "definite_loop"), "回数の決まったくりかえし"),
            (ARRAY, ("sdf", "data_structures", "array"), "配列"),
        ):
            uow.skills.save_kc(KnowledgeComponent(id=kc_id, namespace="cs", path=path, label=label))
        version = uow.tasks.get_version(world.task_version.id)
        fresh = TaskVersionId(new_id("tsv"))
        uow.tasks.save_version(
            version.model_copy(
                update={
                    "id": fresh,
                    "version": version.version + 1,
                    "q_matrix": tuple(
                        QMatrixEntry(task_version_id=fresh, kc_id=kc_id) for kc_id in kc_ids
                    ),
                }
            )
        )
        uow.commit()
    return str(fresh)


def test_the_task_page_names_what_it_asks_about(world: World) -> None:
    """**何を問われているかが分かると、点の理由を結びつけられる。**

    課題ごとの点だけでは、回をまたいで自分の弱いところが言えない。
    """
    world.register("s2400001")
    world.login("s2400001")
    version = _teach(world, LOOPS, ARRAY)

    page = world.client.get(f"/tasks/{version}").text

    assert "この課題で問うこと" in page
    assert "回数の決まったくりかえし" in page
    assert "配列" in page


def test_the_task_page_does_not_carry_the_estimate(world: World) -> None:
    """課題の画面に出るのは**問われること**だけ。

    習熟度は別の画面（`/courses/{id}/mastery`）が受け持つ ── 課題を解きに
    来た人に、まず推定値を読ませない。
    """
    world.register("s2400001")
    world.login("s2400001")
    version = _teach(world, LOOPS)

    page = world.client.get(f"/tasks/{version}").text

    assert "この課題で問うこと" in page
    assert "習熟度" not in page


def test_the_key_is_not_what_the_learner_reads(world: World) -> None:
    """鍵（`cs.sdf...`）は学習者には読めない。表示名で出す。"""
    world.register("s2400001")
    world.login("s2400001")
    version = _teach(world, LOOPS)

    page = world.client.get(f"/tasks/{version}").text

    assert "回数の決まったくりかえし" in page
    # 鍵は `title` 属性には出るが、本文の見出しとしては出ない。
    assert ">cs.sdf.fundamentals.definite_loop<" not in page


def test_a_task_without_components_says_nothing(world: World) -> None:
    """**節ごと出さない。** 「なし」と書くと、書き忘れのように読める。"""
    world.register("s2400001")
    world.login("s2400001")

    page = world.client.get(f"/tasks/{world.task_version.id}").text

    assert "この課題で問うこと" not in page


def test_a_component_missing_from_the_vocabulary_is_not_shown(world: World) -> None:
    """引けなかった KC を鍵のまま並べない ── 意味の無い文字列になる。"""
    world.register("s2400001")
    world.login("s2400001")
    version = _teach(world, LOOPS, MISSING)

    page = world.client.get(f"/tasks/{version}").text

    assert "回数の決まったくりかえし" in page
    assert str(MISSING) not in page


def test_the_submission_page_says_what_was_asked(world: World) -> None:
    """観点は「何で採点したか」、知識要素は「何を問われたか」。

    **採点が済んでから出る。** 観点の結果と同じ塊にあるので、採点前の画面
    には出ない ── そちらは課題の画面が受け持つ（採点を待たずに読める）。
    """
    world.register("s2400001")
    world.login("s2400001")
    version = _teach(world, LOOPS)
    # **知識要素を付けた版に出す。** `world.submit()` は最初の版に出すので、
    # そちらには Q-matrix が無い。
    response = world.client.post(
        f"/tasks/{version}/submit",
        files={"upload": ("main.c", b"int main(void){return 0;}\n", "text/plain")},
        follow_redirects=False,
    )
    submission = response.headers["location"].rsplit("/", 1)[-1]
    world.worker.run_until_empty()

    page = world.client.get(f"/submissions/{submission}").text

    assert "問われたこと" in page
    assert "回数の決まったくりかえし" in page
    assert "習熟度" not in page
