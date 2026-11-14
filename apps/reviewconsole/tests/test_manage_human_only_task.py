"""人が採点するだけの問題を画面から作れる（2026-11-13）。

試験の欠席届のように、機械も AI も採点しない（教員が読んで確かめるだけの）問題を
問題セットに足そうとすると、「設定できない項目」の類のエラーで保存できなかった。
"""

from __future__ import annotations

from html.parser import HTMLParser

from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import HUMAN_SCORED, Role
from aijudge_core.ids import TaskId

STATEMENT = "## [必須] 欠席届 ##\n\n欠席の理由を書き、証明書の画像を添えて提出してください。"


def _latest(world: World):
    with world.database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(world.course.id)
        assert len(tasks) == 1
        return tasks[0], uow.tasks.latest_version(TaskId(tasks[0].id))


def _add(world: World, **extra: object):
    client = world.client("teacher")
    data: dict[str, object] = {
        "key_suffix": "p1",
        "unit": "exam08-abs",
        "statement": STATEMENT,
        "readability_weight": "0.3",
        "no_auto_tests": "1",
    }
    data.update(extra)
    return client, client.post(
        f"/manage/courses/{world.course.id}/tasks", data=data, follow_redirects=False
    )


def _human_rows(statement: str = STATEMENT) -> dict[str, object]:
    return {
        "statement": statement,
        "criterion_code": ["review"],
        "criterion_title": ["欠席理由と証明書の確認"],
        "criterion_description": ["理由と証明書を教員が確かめる。"],
        "criterion_weight": ["1.0"],
        "criterion_evaluator": [HUMAN_SCORED],
        "criterion_levels": [""],
    }


class _FormFields(HTMLParser):
    """画面のフォームが送る値を、ブラウザと同じ規則で集める（最初の保存フォームだけ）。"""

    def __init__(self, action_suffix: str) -> None:
        super().__init__(convert_charrefs=True)
        self._suffix = action_suffix
        self._inside = False
        self._done = False
        self._textarea: str | None = None
        self._select: str | None = None
        self._select_value: str | None = None
        self._select_first: str | None = None
        self._option_value: str | None = None
        self.fields: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
        a = dict(attrs)
        if tag == "form" and not self._done and (a.get("action") or "").endswith(self._suffix):
            self._inside = True
        if not self._inside:
            return
        if tag == "template":
            # 共通ルーブリックに戻すときの中身。ブラウザは送らない。
            self._inside = False
            self._in_template = True
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            if kind in ("submit", "button", "file"):
                return
            self.fields.append((a["name"], a.get("value") or ""))
        elif tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            self.fields.append((a["name"], ""))
        elif tag == "select" and a.get("name"):
            self._select, self._select_value, self._select_first = a["name"], None, None
        elif tag == "option" and self._select is not None:
            value = a.get("value") or ""
            if self._select_first is None:
                self._select_first = value
            if "selected" in a:
                self._select_value = value

    def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
        if tag == "template" and getattr(self, "_in_template", False):
            self._in_template = False
            self._inside = True
            return
        if not self._inside:
            return
        if tag == "textarea":
            self._textarea = None
        elif tag == "select" and self._select is not None:
            chosen = self._select_value if self._select_value is not None else self._select_first
            self.fields.append((self._select, chosen or ""))
            self._select = None
        elif tag == "form":
            self._inside = False
            self._done = True

    def handle_data(self, data):  # type: ignore[no-untyped-def]
        if self._inside and self._textarea is not None:
            name, value = self.fields[-1]
            self.fields[-1] = (name, value + data)


def _form_of(page: str, action_suffix: str) -> list[tuple[str, str]]:
    parser = _FormFields(action_suffix)
    parser.feed(page)
    assert parser.fields, "保存フォームが見つからない"
    return parser.fields


def _as_data(fields: list[tuple[str, str]]) -> dict[str, list[str]]:
    data: dict[str, list[str]] = {}
    for name, value in fields:
        data.setdefault(name, []).append(value)
    return data


def test_the_edit_form_as_a_browser_sends_it_saves_a_human_only_rubric(world: World) -> None:
    """**画面が送るとおりの値で**、既存の観点を全部「人が採点する」に切り替える。"""
    world.register("teacher", Role.INSTRUCTOR)
    client, response = _add(world)
    assert response.status_code == 303, response.text
    task, _ = _latest(world)

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    data = _as_data(_form_of(page, "/revise"))
    codes = data["criterion_code"]
    # 既存の観点（最後は空の「追加」行）をすべて人の採点にする
    data["criterion_evaluator"] = [
        HUMAN_SCORED if code else value
        for code, value in zip(codes, data["criterion_evaluator"], strict=True)
    ]
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data=data,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    _, version = _latest(world)
    assert version.criteria and all(c.scored_by_human for c in version.criteria)


def _open_unit(world: World, client, unit: str = "exam08-abs") -> None:
    # 問題セットを作る（画面の手順どおり。問題の無いセットはまだ記録を持たない）
    response = client.post(
        f"/manage/courses/{world.course.id}/units", data={"unit": unit}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def _new_page(world: World, client, kind: str = "", unit: str = "exam08-abs") -> str:
    query = f"?kind={kind}" if kind else ""
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}/tasks/new{query}")
    assert page.status_code == 200, page.text
    return page.text


def _new_form(world: World, client, kind: str = "") -> dict[str, list[str]]:
    _open_unit(world, client)
    page = _new_page(world, client, kind)
    return _as_data(_form_of(page, f"/manage/courses/{world.course.id}/tasks"))


class _NoGeneration:
    """テストの生成が呼ばれたら落とす。人・AI の問題では生成しないことを確かめる。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def write(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("テストを生成してはいけない")


def test_a_human_only_task_is_created_from_the_new_task_form(world: World, monkeypatch) -> None:
    """**人が採点**を選んで、画面が送るとおりに保存する（本番 2026-11-13 17:28 の 400）。

    以前は新規作成が観点の欄を読まず、テストで確定できる科目では欠席届にも参照解答と
    テストを作らせようとした。いまは画面の観点で作り、テストは作らない。
    """
    import aijudge_reviewconsole.manage.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "TestCaseWriter", _NoGeneration)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    data = _new_form(world, client, kind="human")
    assert data["kind"] == ["human"]
    data["key_suffix"] = ["absence"]
    data["statement"] = [STATEMENT]
    data["suffix"] = [".md", ".png", ".jpg", ".jpeg"]
    data["formats"] = ["1"]
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks", data=data, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    # テストが無いことを警告しない（人の採点ではそれが正常）
    assert "saved=task#" in response.headers["location"]
    task, version = _latest(world)
    assert [c.code for c in version.criteria] == ["review"]
    assert all(c.scored_by_human for c in version.criteria)
    assert version.test_cases == ()
    assert set(task.accepted_suffixes) == {".md", ".png", ".jpg", ".jpeg"}

    # 編集画面は「テストケースが無い」と言わない（正しさの観点が無い）
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "この課題にはテストケースがありません" not in page


def test_the_human_page_shows_only_what_human_scoring_needs(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _open_unit(world, client)
    page = _new_page(world, client, kind="human")
    # テスト生成も、AI にだけ渡す参照回答例も出さない
    assert 'name="test_case_count"' not in page
    assert 'name="no_auto_tests"' not in page
    assert 'name="reference_answer"' not in page
    # 観点は「人が採点する」1 つから書き始める
    fields = _as_data(_form_of(page, f"/manage/courses/{world.course.id}/tasks"))
    assert fields["criterion_code"][0] == "review"
    assert fields["criterion_evaluator"][0] == HUMAN_SCORED


def test_the_program_page_keeps_the_old_shape_without_an_ignored_rubric(world: World) -> None:
    """テストで確定できる科目の既定はプログラム。**観点の欄は出さない**（保存で読まないため）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _open_unit(world, client)
    page = _new_page(world, client)
    assert 'name="test_case_count"' in page
    fields = _as_data(_form_of(page, f"/manage/courses/{world.course.id}/tasks"))
    assert fields["kind"] == ["program"]
    assert "criterion_code" not in fields


def test_an_ai_task_is_created_with_the_rubric_on_the_form(world: World, monkeypatch) -> None:
    import aijudge_reviewconsole.manage.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "TestCaseWriter", _NoGeneration)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    data = _new_form(world, client, kind="ai")
    data["key_suffix"] = ["essay"]
    data["statement"] = ["## [必須] 感想 ##\n\n感想を書く。"]
    data["criterion_title"][0] = "感想の具体性"
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks", data=data, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    _, version = _latest(world)
    assert [(c.code, c.title) for c in version.criteria] == [("content", "感想の具体性")]
    assert not version.criteria[0].scored_by_human
    assert version.test_cases == ()


def test_a_key_with_unusable_characters_is_refused_before_generating(
    world: World, monkeypatch
) -> None:
    """キーの検査はテストの生成より前（以前は生成を待たされた末に断られた）。"""
    import aijudge_reviewconsole.manage.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "TestCaseWriter", _NoGeneration)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    data = _new_form(world, client)  # プログラム（生成する側）
    data["key_suffix"] = ["欠席届"]
    data["statement"] = [STATEMENT]
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks", data=data, follow_redirects=False
    )
    assert response.status_code == 400
    assert "使えない文字" in response.text


def test_an_empty_unit_offers_no_settings_and_says_why(world: World) -> None:
    """問題の無いセットには設定の入れ先が無い（本番 2026-11-13 17:23 の 404）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _open_unit(world, client)
    page = client.get(f"/manage/courses/{world.course.id}/units/exam08-abs").text
    settings_action = f'action="/manage/courses/{world.course.id}/units/exam08-abs/settings"'
    assert settings_action not in page
    assert "最初の問題を追加したあと" in page

    response = client.post(
        f"/manage/courses/{world.course.id}/units/exam08-abs/settings",
        data={"confidential": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "最初の問題を追加してから" in response.text


def test_switching_a_new_task_to_human_only_is_saved(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client, response = _add(world)
    assert response.status_code == 303, response.text
    task, _ = _latest(world)

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data=_human_rows(),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    _, version = _latest(world)
    assert [c.code for c in version.criteria] == ["review"]
    assert all(c.scored_by_human for c in version.criteria)
