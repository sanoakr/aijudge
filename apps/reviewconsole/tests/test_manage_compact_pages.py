"""設定の画面を詰めて、一覧で読めるようにする（2026-09-26）。

固定したいのは 3 つ。

2 段の一覧       問題セットの行は「どのセットか」と「問数・日程」の 2 段。
                 1 行に 7 列を並べていたので、日付が入ると幅が足りずに折り返した。
同じ時刻は 1 度  開始が公開と同時なら書かない。日程が無ければ 1 語で言う。
控えめな取り消し  受講の取り消しは畳んだ小さな文字から開き、押すときに確かめる。
                 学期中にまず使わない操作が、各行に朱のボタンで並んでいた。
"""

from __future__ import annotations

import re

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role


def _unit_row(page: str, unit: str) -> str:
    match = re.search(rf'<a class="row[^"]*"\s+href="[^"]*/units/{unit}">(.*?)</a>', page, re.S)
    assert match, f"{unit} の行が一覧に無い"
    return match.group(1)


def test_a_unit_is_listed_on_two_lines(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    row = _unit_row(world.client("teacher").get(f"/courses/{world.course.id}").text, unit)

    assert 'class="s-main"' in row
    assert 'class="s-meta desc"' in row
    # 日程が無ければ「未設定」を 3 度並べず、1 語で言う。
    assert "日程未設定" in row
    assert "公開" not in row


def test_the_start_is_omitted_when_it_is_the_opening(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"opens_at": "2026-10-01T10:40", "due_at": "2026-10-15T23:59"},
        follow_redirects=False,
    )

    row = _unit_row(client.get(f"/courses/{world.course.id}").text, unit)

    assert "公開 10-01 10:40" in row
    assert "締切 10-15 23:59" in row
    assert "開始" not in row
    assert "日程未設定" not in row


def test_removing_an_enrolment_is_folded_and_confirmed(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    folded = re.search(r'<details class="quiet-danger">(.*?)</details>', page, re.S)
    assert folded, "取り消しが畳まれていない"
    assert f"/enrolments/{learner.user_id}/remove" in folded.group(1)
    assert "data-confirm=" in folded.group(1)
    # 畳んだ外側に、取り消しのボタンが出ていない。
    outside = page.replace(folded.group(0), "")
    assert f"/enrolments/{learner.user_id}/remove" not in outside


# --------------------------------------------------------------------------
# 説明を畳む（`_help.html`）
# --------------------------------------------------------------------------


def test_the_help_keeps_its_text_in_the_page() -> None:
    """**隠すのは見た目だけ。** 説明の文字は HTML に残り、押しても載せても開く。"""
    from aijudge_reviewconsole.app import TEMPLATES

    html = TEMPLATES.env.from_string(
        '{% from "_help.html" import help %}'
        "<label>名前{% call help() %}<strong>理由</strong>の説明{% endcall %}</label>"
    ).render()

    assert '<button type="button" class="help-btn"' in html
    assert 'aria-expanded="false"' in html
    assert '<span class="help-body" role="note"><strong>理由</strong>の説明</span>' in html


def test_the_roles_are_explained_behind_the_help(world: World) -> None:
    """役割の表は 4 行の説明で、登録の操作を画面の下に押し出していた。"""
    world.register("teacher", Role.INSTRUCTOR)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    body = re.search(r'<span class="help-body" role="note">(.*?)</span></span>', page, re.S)
    assert body
    folded = "".join(
        m.group(1)
        for m in re.finditer(r'<span class="help-body" role="note">(.*?)</span></span>', page, re.S)
    )
    assert "下位の役割に加えてできること" in folded
    assert "aijudge-admin enrol" in folded


# --------------------------------------------------------------------------
# 問題のページ: 内容と操作をタブで分ける
# --------------------------------------------------------------------------


def _task_page(world: World) -> str:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    return (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )


def test_the_task_page_separates_content_from_operations(world: World) -> None:
    """保存すると版になる内容と、押すとその場で起きる操作を、別のタブに置く。"""
    page = _task_page(world)

    assert "data-tabs" in page
    content = page[page.index('id="tab-content"') : page.index('id="tab-ops"')]
    ops = page[page.index('id="tab-ops"') :]
    assert "/revise" in content, "内容の保存は内容のタブにある"
    assert 'id="schedule"' in ops, "日程は操作のタブにある（#schedule で開く）"
    assert "/delete" in ops and "/delete" not in content


def test_the_content_form_keeps_what_was_typed(world: World) -> None:
    """内容のフォームはまとめて保存の作法に乗る（断られても書きかけを消さない）。
    KC の候補は頁を移らずに差し込む ── 頁ごと描き直すと観点の書きかけが消えた。"""
    page = _task_page(world)

    assert re.search(r'<form method="post" data-save-all', page)
    assert 'data-swap="#kc-candidates"' in page
    assert 'id="kc-candidates"' in page


# --------------------------------------------------------------------------
# 知識要素の選び方（`.kcpick` の拡張・base.html）
# --------------------------------------------------------------------------


def test_the_kc_picker_helpers_are_never_submitted() -> None:
    """絞り込み欄と「選択中だけ」は**送らない**。名前を持たせると、フォームの
    保存に紛れ込み、書きかけの印（未保存の変更）まで立てる。"""
    from pathlib import Path

    import aijudge_reviewconsole

    base = (Path(aijudge_reviewconsole.__file__).parent / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    start = base.index('document.querySelectorAll(".kcpick")')
    script = base[start : base.index("})();", start)]
    assert ".name =" not in script
    assert 'setAttribute("name"' not in script
    # 送られない欄の入力は、書きかけに数えない。
    assert "!event.target.name" in base
