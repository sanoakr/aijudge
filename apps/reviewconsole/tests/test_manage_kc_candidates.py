"""課題の編集画面の「AI に候補を出させる」（#496）。

追加の候補は根拠と一緒に、削除の候補は理由と一緒に出す。付け外しは教員が決める。
**削除の候補にはチェック欄を置かない** ── 外すのは上の一覧の印で、同じ値の欄が
2 つあると片方だけ外しても値が送られてしまう。
"""

from __future__ import annotations

from types import SimpleNamespace

from test_manage import World, _import_example
from test_manage import world as world  # フィクスチャを借りる

from aijudge_admin.syllabus import KcNotUsed, KcUse, TaskKcResult
from aijudge_core import Role

BRANCHING = "cs.sdf.fundamentals.branching"
CONSOLE_IO = "cs.sdf.fundamentals.console_io"
COMPILE_LINK = "cs.fpl.translation.compile_link"


def test_candidates_come_with_their_evidence_and_reasons(world: World, monkeypatch) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        uow.identity.save_course(
            course.model_copy(
                update={"knowledge_components": (BRANCHING, CONSOLE_IO, COMPILE_LINK)}
            )
        )
        uow.commit()
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.list_for_namespaces",
        lambda database, namespaces, include_deprecated: [
            SimpleNamespace(key=BRANCHING, label="分岐"),
            SimpleNamespace(key=CONSOLE_IO, label="コンソール入出力"),
            SimpleNamespace(key=COMPILE_LINK, label="コンパイルとリンク"),
        ],
    )
    seen: dict = {}

    def select(self, statement, *, vocabulary, current, reference_solution):
        seen.update(vocabulary=vocabulary, current=current)
        return TaskKcResult(
            add=(
                KcUse(key=BRANCHING, evidence="if (n>0) ... else if (n<0)"),
                KcUse(key=CONSOLE_IO, evidence="scanf と printf"),
            ),
            remove=(KcNotUsed(key=COMPILE_LINK, reason="入出力と分岐だけの課題"),),
            discarded=(),
            prompt_id="task_to_kcs_ja@1",
            model="test",
        )

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskKcReader.select", select)

    page = (
        world.client("teacher")
        .post(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/kc-candidates",
            data={
                "statement": "キーボードから入力した整数値の正負を判定し出力しなさい。" * 2,
                "kc": [CONSOLE_IO, COMPILE_LINK],
            },
        )
        .text
    )
    candidates = page[page.index('id="kc-candidates"') :]

    # 渡したもの: コースの知識要素（名前つき）と、いま画面で選んでいるもの。
    assert seen["vocabulary"][BRANCHING] == "分岐"
    assert seen["current"] == (CONSOLE_IO, COMPILE_LINK)
    # 追加の候補は根拠つき。付いていないものだけに印の欄を置く。
    assert "if (n&gt;0) ... else if (n&lt;0)" in candidates
    assert f'name="kc" value="{BRANCHING}"' in candidates
    assert f'name="kc" value="{CONSOLE_IO}"' not in candidates, "付いているものに欄が重なる"
    assert "既に付いています" in candidates
    # 削除の候補は理由つき、欄は置かない。
    assert "削除の候補" in candidates
    assert "入出力と分岐だけの課題" in candidates
    assert f'name="kc" value="{COMPILE_LINK}"' not in candidates
