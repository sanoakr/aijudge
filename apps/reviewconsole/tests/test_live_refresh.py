"""教員画面の自動更新（`live.js`・2026-10-08）。

残数のバッジや一覧が、開き直さないと変わらなかった。`data-live` を付けた区画だけを
定期的に取り直して差し替える。サーバ側で固定したいのは 4 つ。

知らせを消さない      自動更新の取得（`X-Aijudge-Live`）は、一度だけ出す知らせを取り出さない
区画が画面にある      残数・一覧を持つ画面は、差し替える区画を持つ
区画の名前は一意      同じ名前が 2 つあると、取り直した側のどれを当てるか決まらない
行の鍵は一意          鍵が重なると、行の突き合わせが別の行を消す
"""

from __future__ import annotations

import re
from collections import Counter

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_reviewconsole import notices
from aijudge_reviewconsole.notices import FINALIZED, Notices

LIVE = {"X-Aijudge-Live": "1"}


def test_a_live_poll_reads_a_notice_without_consuming_it() -> None:
    store = Notices()
    store.put("usr_a", "crs_1", FINALIZED, {"finalized": 3})

    token = notices.live_poll.set(True)
    try:
        assert store.take("usr_a", "crs_1", FINALIZED) == {"finalized": 3}
        assert store.take("usr_a", "crs_1", FINALIZED) == {"finalized": 3}, "取得が消した"
    finally:
        notices.live_poll.reset(token)

    assert store.take("usr_a", "crs_1", FINALIZED) == {"finalized": 3}
    assert store.take("usr_a", "crs_1", FINALIZED) is None, "普通の表示は一度で消える"


def test_the_finalize_page_keeps_its_notice_through_a_live_poll(world: World) -> None:
    """**操作の結果の知らせが、裏の取得に取られない。**"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    world.console.notices.put(
        teacher.user_id,
        world.course.id,
        FINALIZED,
        {"finalized": 7, "contested": 0, "ai_pending": 0, "awaiting_human": 0},
    )
    url = f"/courses/{world.course.id}/finalize"

    polled = client.get(url, headers=LIVE).text
    assert "7 件を確定しました" in polled
    assert "7 件を確定しました" in client.get(url, headers=LIVE).text, "取得で知らせが消えた"

    assert "7 件を確定しました" in client.get(url).text
    assert "7 件を確定しました" not in client.get(url).text, "普通の表示のあとも残っている"


def _pages(world: World) -> dict[str, str]:
    _import_example(world)
    unit = _unit_of(world)
    base = f"/courses/{world.course.id}"
    return {
        "index": "/",
        "units": f"/courses/{world.course.id}",
        "finalize": f"{base}/finalize",
        "queue": f"{base}/queue",
        "submissions": f"{base}/submissions",
        "unit": f"/manage/courses/{world.course.id}/units/{unit}",
    }


def test_pages_with_counts_or_lists_carry_live_regions(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    pages = _pages(world)
    expected = {
        "index": "digest-",
        "units": "cm-open-",
        "finalize": "fin-summary",
        "queue": "queue-rows",
        "submissions": "sub-rows",
        "unit": "mu-banner",
    }
    for name, marker in expected.items():
        html = client.get(pages[name]).text
        assert f'data-live="{marker}' in html, f"{name} に差し替える区画（{marker}）が無い"
        assert "live.js" in html, f"{name} が自動更新の script を読んでいない"


def test_region_names_and_row_keys_are_unique_on_each_page(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    pages = _pages(world)
    for name in ("index", "units", "finalize", "queue", "submissions", "unit"):
        html = client.get(pages[name]).text
        names = re.findall(r'data-live="([^"]+)"', html)
        duplicated = [n for n, count in Counter(names).items() if count > 1]
        assert not duplicated, f"{name}: 区画の名前が重なっている {duplicated}"
        for region in re.findall(
            r'<(tbody|div)[^>]*data-live="[^"]+"[^>]*data-live-rows[^>]*>(.*?)</\1>', html, re.S
        ):
            keys = re.findall(r'data-live-key="([^"]+)"', region[1])
            repeated = [k for k, count in Counter(keys).items() if count > 1]
            assert not repeated, f"{name}: 行の鍵が重なっている {repeated}"
