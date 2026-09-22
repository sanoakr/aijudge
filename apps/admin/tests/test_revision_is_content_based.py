"""訂正の判定は**内容**で行う。同一性（ID と版番号）で行うと常に「違う」になる。

`save_task(revise=True)` は「内容が同じなら版は上がらない（何度押しても
増えない）」と宣言しているが、比較に `substantive` を使っていたあいだ、
**その宣言は成り立っていなかった**。`substantive` は `id` と `version` を
含み、候補は必ず版 1 として組まれる（`build_task_version` の既定）ので、
版 2 以上の課題と比べれば必ず食い違う。

実測（2026-09-22）で、同じ内容の訂正を 3 回流すと版は 1 → 2 → 3 と増えた。
影響は 3 か所に出ていた。

- `course apply --revise` を流し直すたび、**直していない課題も含めて全件**の
  版が 1 つ増える。「何度流しても増えない」という定義ファイルの性質
  （`course_definition` 冒頭）が `--revise` を付けた瞬間に失われる。
- 教員コンソールの「この問題を保存して更新する」が、何も直さずに押しただけで
  版を作る。
- 束の取り込み前に変化を数える `plan_bundle` が、版 2 以上の課題を全部
  「訂正される」と数える ── 教員は 1 件だけ直したつもりで 130 件の訂正を
  見せられる。

版が無駄に増えること自体は P8（追記のみ）に反しないが、**どの版が実際の
訂正なのかが記録から読めなくなる**。`course diff`（#363）はこの比較の上に
載るので、ここが直っていないと「ファイルと DB がずれている」を常に報告する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin import PlannedChange, ensure_course, plan_bundle, save_task
from aijudge_authoring import TaskSpec
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング及び実習2",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    yield database, course
    database.dispose()


def _spec(statement: str) -> TaskSpec:
    return TaskSpec(key="ex01/p1", statement=statement, unit="ex01")


def _save(database, course, statement: str, *, revise: bool = True):
    return save_task(
        database,
        course_id=course.id,
        spec=_spec(statement),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        revise=revise,
    )


def test_revising_without_changing_anything_keeps_the_version(world) -> None:
    database, course = world
    first = _save(database, course, "## 問題 ##\n本文", revise=False)
    again = _save(database, course, "## 問題 ##\n本文")
    third = _save(database, course, "## 問題 ##\n本文")

    assert (first.version.version, again.version.version, third.version.version) == (1, 1, 1)
    assert again.version.id == first.version.id


def test_revising_the_statement_raises_the_version_once_per_change(world) -> None:
    """**直したときは増える。** 直っていないときだけ増えない。"""
    database, course = world
    _save(database, course, "## 問題 ##\n本文", revise=False)
    revised = _save(database, course, "## 問題 ##\n書き直した本文")
    unchanged = _save(database, course, "## 問題 ##\n書き直した本文")
    again = _save(database, course, "## 問題 ##\nもう一度書き直した")

    assert revised.version.version == 2
    assert unchanged.version.version == 2
    assert again.version.version == 3


def test_planning_a_bundle_counts_a_revised_task_as_unchanged(world) -> None:
    """数える側も同じ規則にする（`plan_bundle` の docstring）。

    版 2 の課題を同じ内容で数えて「訂正される」と出ると、教員は 1 件直した
    つもりで束の全件が訂正対象に見える。
    """
    database, course = world
    _save(database, course, "## 問題 ##\n本文", revise=False)
    _save(database, course, "## 問題 ##\n書き直した本文")

    planned = plan_bundle(
        database,
        course_id=course.id,
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        specs=(_spec("## 問題 ##\n書き直した本文"),),
    )
    assert [item.change for item in planned] == [PlannedChange.UNCHANGED]

    planned = plan_bundle(
        database,
        course_id=course.id,
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        specs=(_spec("## 問題 ##\nさらに直した"),),
    )
    assert [item.change for item in planned] == [PlannedChange.REVISED]
