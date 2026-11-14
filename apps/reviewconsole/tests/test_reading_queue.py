"""「人が読む」── blind 採点と手動確定の推奨を 1 つの画面に並べる（2026-11-14）。

手動確定が必須でなくなり（ADR 0034）、どちらも教員が任意で読む列になったので画面を
まとめた。**まとめたのは画面だけ**で、固定したいのは次のこと。

開き先は分けたまま   blind の行は AI を見ずに付ける画面へ、推奨の行は確定の画面へ
両方に当たれば blind  推奨の行でも、抽出に当たっていれば blind の画面へつなぐ
判定を一覧に出さない  blind の待ちが同じ一覧に並ぶ（#695）
古い URL は転送する   /manual と /blind はブックマークに残っている
"""

from __future__ import annotations

from test_console import (
    AI_RATIONALE,
    COURSE,
    World,
    _hold_results,
    _instructor_and_submission,
    needs_c_compiler,
)
from test_console import blind_world as blind_world  # フィクスチャを借りる
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role


@needs_c_compiler
def test_a_sampled_submission_opens_the_blind_page(blind_world: World) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    sid = accepted.submission.id

    page = blind_world.client.get(f"/courses/{COURSE}/read").text

    assert f"/review/{sid}/blind?from=blind" in page
    assert "blind 採点 1 件" in page
    assert AI_RATIONALE not in page, "blind の待ちが並ぶ一覧に AI の判定が出ている"


@needs_c_compiler
def test_a_recommended_submission_opens_the_finalize_page(world: World) -> None:
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    _hold_results(world)
    sid = accepted.submission.id

    page = world.client.get(f"/courses/{COURSE}/read").text

    assert f"/review/{sid}/reveal?from=finalize" in page
    assert "手動確定を推奨 1 件" in page
    assert "blind 採点を待っている提出はありません" in page


@needs_c_compiler
def test_a_submission_in_both_lists_is_blind_first(blind_world: World) -> None:
    """抽出に当たった提出は、推奨の行からも blind の画面へつなぐ。

    確定の画面を開いても抽出の門が blind へ回すので、先に AI を見せる行き先を
    並べない。
    """
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    _hold_results(blind_world)
    sid = accepted.submission.id

    page = blind_world.client.get(f"/courses/{COURSE}/read").text

    assert "先に blind 採点" in page
    assert f"/review/{sid}/reveal?from=finalize" not in page.split("<tbody>", 1)[1]
    assert page.index("blind 採点</span>") < page.index("先に blind 採点")


def test_the_old_lists_forward_to_the_combined_page(world: World) -> None:
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    for old in ("manual", "blind"):
        response = world.client.get(f"/courses/{COURSE}/{old}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].endswith(f"/courses/{COURSE}/read")


def test_the_rail_has_one_entry_for_both(world: World) -> None:
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")

    page = world.client.get(f"/courses/{COURSE}/read").text

    assert f'/courses/{COURSE}/read"' in page
    assert f'/courses/{COURSE}/blind"' not in page
    assert f'/courses/{COURSE}/manual"' not in page
