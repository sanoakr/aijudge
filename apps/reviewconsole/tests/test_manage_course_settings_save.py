"""共通設定をまとめて保存する（2026-09-26）。

以前は自動確定・提出形式・共通ルーブリック・採点設定がそれぞれ別のフォームで、
1 つを保存すると他の書きかけが消えた。固定したいのは 4 つ。

開いて送れば変化なし  画面の値をそのまま送ると、何も書かない（既定のルーブリックが
                      明示の宣言に化けない）。**画面から欄を拾って**確かめる。
変えた節だけ書く      自動確定の分数だけ変えれば、それだけが変わる。
1 つ断られたら全部    形式を全部外したら、同時に送った分数も書かれない。
試行は保存しない      まとめて保存のフォームの試行ボタンは、採点設定を書かない。
"""

from __future__ import annotations

from html.parser import HTMLParser

from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role


class _FormFields(HTMLParser):
    """`form#<id>` の中の送られる欄を、ブラウザと同じ規則で拾う（素朴な版）。"""

    def __init__(self, form_id: str) -> None:
        super().__init__()
        self.form_id = form_id
        self.inside = False
        self.fields: list[tuple[str, str]] = []
        self._select: str | None = None
        self._select_value: str | None = None
        self._textarea: str | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form" and a.get("id") == self.form_id:
            self.inside = True
            return
        if not self.inside:
            return
        name = a.get("name")
        if tag == "input" and name and "disabled" not in a:
            kind = a.get("type", "text")
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((name, a.get("value", "on")))
            elif kind not in ("submit", "button", "file"):
                self.fields.append((name, a.get("value", "")))
        elif tag == "select" and name:
            self._select, self._select_value = name, None
        elif tag == "option" and self._select is not None:
            if self._select_value is None or "selected" in a:
                self._select_value = a.get("value", "")
        elif tag == "textarea" and name:
            self._textarea, self._text = name, ""

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.inside:
            self.inside = False
        elif tag == "select" and self._select is not None:
            self.fields.append((self._select, self._select_value or ""))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            # ブラウザは先頭の改行 1 つを落とす。
            self.fields.append((self._textarea, self._text.removeprefix("\n")))
            self._textarea = None


def _as_shown(world: World, client) -> dict[str, list[str]]:
    page = client.get(f"/manage/courses/{world.course.id}").text
    parser = _FormFields("course-settings")
    parser.feed(page)
    form: dict[str, list[str]] = {}
    for name, value in parser.fields:
        form.setdefault(name, []).append(value)
    assert form, "共通設定のフォームが見つからない"
    return form


def _course(world: World):
    with world.database.unit_of_work() as uow:
        return uow.identity.get_course(world.course.id)


def _settings(world: World) -> str:
    return f"/manage/courses/{world.course.id}/settings"


def test_the_page_has_one_form_and_one_save_button(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    assert page.count('/settings"') == 1
    for gone in ("/auto-finalize", "/upload-formats", "/rubric"):
        assert f'/manage/courses/{world.course.id}{gone}"' not in page


def test_saving_what_is_shown_changes_nothing(world: World) -> None:
    """**既定のルーブリックが明示の宣言に化けない。** 画面をそのまま送る。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    before = _course(world)

    response = client.post(_settings(world), data=_as_shown(world, client), follow_redirects=False)

    assert "saved=unchanged" in response.headers["location"]
    after = _course(world)
    assert after.rubric == before.rubric
    assert after.grading_overrides == before.grading_overrides
    assert after.upload_suffixes == before.upload_suffixes


def test_only_the_changed_section_is_written(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    before = _course(world)
    form = _as_shown(world, client)
    form["after_minutes"] = ["90"]

    response = client.post(_settings(world), data=form, follow_redirects=False)

    assert "saved=course_settings" in response.headers["location"]
    after = _course(world)
    assert after.auto_finalize_after_minutes == 90
    assert after.rubric == before.rubric
    assert after.grading_overrides == before.grading_overrides


def test_a_refusal_writes_nothing(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    form = _as_shown(world, client)
    form["after_minutes"] = ["90"]
    form.pop("suffix", None)

    response = client.post(_settings(world), data=form, follow_redirects=False)

    assert response.status_code == 400
    assert _course(world).auto_finalize_after_minutes is None


def test_the_trial_button_does_not_save(world: World) -> None:
    """試行はまとめて保存のフォームから `?action=try` で送る。**保存しない。**"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    page = client.get(f"/manage/courses/{world.course.id}").text
    assert "/grading?action=try" in page
    form = _as_shown(world, client)
    form["case_timeout_seconds"] = ["4"]

    response = client.post(f"/manage/courses/{world.course.id}/grading?action=try", data=form)

    assert response.status_code == 200
    assert 'id="trial-result"' in response.text
    assert _course(world).grading_overrides == {}


def test_saving_the_unit_page_as_shown_changes_nothing(world: World) -> None:
    """問題セットの画面も、開いてそのまま送れば何も書かない（画面から欄を拾う）。"""
    from test_manage import _import_example, _unit_of

    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/settings",
        data={"file_upload": "1", "opens_at": "2026-10-01T10:40", "due_at": "2026-10-15T23:59"},
    )
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    parser = _FormFields("unit-settings")
    parser.feed(page)
    form: dict[str, list[str]] = {}
    for name, value in parser.fields:
        form.setdefault(name, []).append(value)

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/settings",
        data=form,
        follow_redirects=False,
    )

    assert "saved=unchanged" in response.headers["location"]
