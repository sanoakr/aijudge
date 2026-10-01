"""左の帯の「blind 採点」に残りの件数を出す（2026-10-01）。

件数が無いと、どれだけ残っているかを開いて数えることになる。固定したいのは 3 つ。

一覧と同じ数   帯の数字と、開いた先の待ちの件数が同じ
付ければ減る   blind 採点を付けると数字が減り、0 なら出さない
注意の色で出す  残りがあれば赤（`attn`）。灰色では目に入らなかった
"""

from __future__ import annotations

import re

from test_console import COURSE, World, _agree_form, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる

from aijudge_core import Role


def _rail_blind(body: str) -> str | None:
    """帯の「blind 採点」の行に付いた数字の要素（無ければ None）。"""
    item = re.search(r'href="[^"]*/courses/[^"]+/blind"[^>]*>(.*?)</a>', body, re.S)
    assert item is not None, "帯に blind 採点の行が無い"
    count = re.search(r'<span class="c[^"]*">\d+</span>', item.group(1))
    return None if count is None else count.group(0)


@needs_c_compiler
def test_the_rail_counts_the_blind_marks_left(blind_world: World) -> None:
    first = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.submit(blind_world.register("s2400002", role=Role.LEARNER))
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()

    body = blind_world.client.get(f"/courses/{COURSE}/blind").text
    assert _rail_blind(body) == '<span class="c attn">2</span>', "一覧と同じ 2 件・注意の色"

    with blind_world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(first.submission.id)
    form = _agree_form(blind_world, {s.criterion_id: s.level for s in run.criterion_scores})
    form.pop("comment")
    blind_world.client.post(f"/review/{first.submission.id}/blind", data=form)

    body = blind_world.client.get(f"/courses/{COURSE}/blind").text
    assert _rail_blind(body) == '<span class="c attn">1</span>'


@needs_c_compiler
def test_an_instructors_trial_is_not_counted(blind_world: World) -> None:
    """教員自身の試行は抽出の母集団に入らない（#108）。帯も数えない。"""
    teacher = blind_world.register("instructor", role=Role.INSTRUCTOR)
    blind_world.submissions.accept(
        tenant_id=_tenant(),
        task_version_id=blind_world.task_version.id,
        learner_id=teacher.user_id,
        submitted_as=Role.INSTRUCTOR,
        subject_profile=blind_world.task_version.subject_profile,
        files=_files(),
    )
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()

    body = blind_world.client.get(f"/courses/{COURSE}/blind").text
    assert _rail_blind(body) is None


def _tenant():
    from test_console import TENANT

    return TENANT


def _files():
    from test_console import EXAMPLE_SOURCE

    from aijudge_core import ArtifactKind
    from aijudge_submission import IncomingFile

    return [
        IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=EXAMPLE_SOURCE.read_bytes())
    ]
