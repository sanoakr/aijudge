"""課題の編集画面は「この問題を保存して更新する」1 つで、検証データも保存する（2026-10-02）。

以前は観点の中の検証データ（入出力セット・項目表・クライアント・サーバのケース）に
別の保存ボタンがあり、片方を押すともう片方で書きかけた内容が黙って消えた。
固定したいのは 4 つ。

1 つで保存       問題文と検証データを同じ保存で直すと、版は 1 つだけ上がる
見たまま送れば   画面の値をそのまま送ると何も変わらない（版も上がらない）
印が無ければ     欄の無い送信（API・問題文だけの訂正）は検証データを引き継ぐ
断られたら       検証データの 1 つが断られたら、問題文も保存しない
"""

from __future__ import annotations

import tempfile
from html.parser import HTMLParser
from pathlib import Path

from test_manage import World
from test_network_comparison import _network_task

from aijudge_core.ids import TaskId


class _TaskForm(HTMLParser):
    """`.../revise` へ送る課題のフォームの欄を、ブラウザと同じ規則で拾う（素朴な版）。"""

    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.fields: list[tuple[str, str]] = []
        self._select: str | None = None
        self._select_value: str | None = None
        self._textarea: str | None = None
        self._text = ""
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "form" and a.get("action", "").endswith("/revise"):
            self.inside = True
            return
        if tag == "template" and self.inside:
            self._depth += 1
        if not self.inside or self._depth:
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

    def handle_data(self, data):
        if self._textarea is not None:
            self._text += data

    def handle_endtag(self, tag):
        if tag == "template" and self._depth:
            self._depth -= 1
            return
        if tag == "form" and self.inside:
            self.inside = False
        elif tag == "select" and self._select is not None:
            self.fields.append((self._select, self._select_value or ""))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            self.fields.append((self._textarea, self._text.removeprefix("\n")))
            self._textarea = None


def _as_shown(world: World, task_id) -> dict[str, list[str]]:
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit")
    parser = _TaskForm()
    parser.feed(page.text)
    form: dict[str, list[str]] = {}
    for name, value in parser.fields:
        form.setdefault(name, []).append(value)
    assert "statement" in form, "課題のフォームが見つからない"
    return form


def _latest(world: World, task_id):
    with world.database.unit_of_work() as uow:
        return uow.tasks.latest_version(TaskId(str(task_id)))


def _revise(world: World, task_id, form):
    return world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data=form,
        follow_redirects=False,
    )


def _world() -> World:
    """クライアント・サーバの評価器を宣言している科目のコース（`cs_network_python`）。"""
    world = World(Path(tempfile.mkdtemp()))
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        uow.identity.save_course(course.model_copy(update={"subject_profile": "cs_network_python"}))
        uow.commit()
    return world


def test_saving_what_is_shown_changes_nothing() -> None:
    world = _world()
    try:
        task_id = _network_task(world, profile="cs_network_python")
        before = _latest(world, task_id)

        response = _revise(world, task_id, _as_shown(world, task_id))

        assert response.status_code == 303, response.text
        after = _latest(world, task_id)
        assert after.version == before.version, "見たまま送ったら版は上がらない"
        assert after.test_cases == before.test_cases
    finally:
        world.close()


def test_the_statement_and_the_client_server_cases_are_saved_together() -> None:
    world = _world()
    try:
        task_id = _network_task(world, profile="cs_network_python")
        before = _latest(world, task_id)
        form = _as_shown(world, task_id)
        form["statement"] = [form["statement"][0] + "\n\n追記。"]
        form["net_port"] = ["50009", *form["net_port"][1:]]

        response = _revise(world, task_id, form)

        assert response.status_code == 303, response.text
        after = _latest(world, task_id)
        assert after.version == before.version + 1, "1 つの保存で版は 1 つだけ上がる"
        assert after.statement.endswith("追記。")
        (case,) = after.test_cases
        assert case.payload["port"] == 50009
        assert case.payload["fixtures"] == {"data.txt": "abc\n"}
    finally:
        world.close()


def test_a_form_without_the_data_fields_keeps_the_data() -> None:
    world = _world()
    try:
        task_id = _network_task(world, profile="cs_network_python")
        before = _latest(world, task_id)
        form = {
            key: value
            for key, value in _as_shown(world, task_id).items()
            if not key.startswith("net_") and key != "companion_present"
        }
        form["statement"] = [form["statement"][0] + "\n\n追記。"]

        _revise(world, task_id, form)

        after = _latest(world, task_id)
        assert after.version == before.version + 1
        assert after.test_cases == before.test_cases, "欄が無ければ引き継ぐ"
    finally:
        world.close()


def test_a_refused_data_field_saves_nothing() -> None:
    world = _world()
    try:
        task_id = _network_task(world, profile="cs_network_python")
        before = _latest(world, task_id)
        form = _as_shown(world, task_id)
        form["statement"] = [form["statement"][0] + "\n\n追記。"]
        form["net_port"] = ["80", *form["net_port"][1:]]

        response = _revise(world, task_id, form)

        assert response.status_code == 400
        assert _latest(world, task_id).version == before.version, "問題文も保存しない"
    finally:
        world.close()


def test_the_io_set_is_saved_with_the_statement() -> None:
    """入出力セットも同じ保存で直る（参照解答が無いので門は通さない）。"""
    from test_manage import _user_id

    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec, TestCaseSpec
    from aijudge_core import Role
    from aijudge_course_admin.authoring import save_task

    world = World(Path(tempfile.mkdtemp()))
    try:
        world.register("teacher", Role.INSTRUCTOR)
        saved = save_task(
            world.database,
            course_id=world.course.id,
            spec=TaskSpec(
                key="ex02/p1",
                unit="ex02",
                statement="## [必須] 足し算 ##\n\n2 つの整数の和を出す。",
                criteria=(
                    CriterionSpec(
                        code="correctness",
                        title="出力の正しさ",
                        description="仕様どおりの出力を返すか。",
                        weight=1.0,
                        evaluator="code_test_runner",
                        levels=(
                            LevelSpec(
                                level=0, label="未達", descriptor="通らない", score_ratio=0.0
                            ),
                            LevelSpec(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                        ),
                    ),
                ),
                test_cases=(TestCaseSpec(name="case1", input="1 2\n", expected="3\n"),),
            ),
            subject_profile="cs_lang_c_intro",
            authored_by=_user_id(world, "teacher"),
        )
        task_id = saved.task.id
        before = _latest(world, task_id)
        form = _as_shown(world, task_id)
        assert form.get("io_present") == ["1"]
        form["case_expected"] = ["4\n", *form["case_expected"][1:]]
        form["case_input"] = ["2 2\n", *form["case_input"][1:]]

        response = _revise(world, task_id, form)

        assert response.status_code == 303, response.text
        after = _latest(world, task_id)
        assert after.version == before.version + 1
        (case,) = after.test_cases
        assert (case.payload["input"], case.payload["expected"]) == ("2 2\n", "4\n")
    finally:
        world.close()
