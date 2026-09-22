"""`content()` は版番号から導かれる ID を、トップレベルだけでなく中まで外す。

`content()` は「同じ内容か」を訊くための取り出しで、`id` と `version` を
比較から外す（`repository.IDENTITY_FIELDS`）。だが `q_matrix` の各行は
`QMatrixEntry.task_version_id` として**同じ素材（版番号）から導かれた ID**を
自分の中に持っており、トップレベルの除外はそこまで届かない。

知識要素（KC）を 1 つでも宣言した課題は `q_matrix` が非空になり、比較の
両辺が違う版番号を指す限り `task_version_id` が必ず食い違う ──
`content()` の存在理由そのものが、KC を使った瞬間に壊れる。

実測（2026-09-22）: `network` コースの知識要素つき課題 9 件中 7 件が、
`statement` も `criteria` も一字一句同じなのに「内容が違う」と判定された。
KC はこの基盤の中核機能なので、これは v1.15.0 で直したはずの「訂正の判定は
内容で行う」を、実運用の課題のほとんどで無効化していた。
"""

from __future__ import annotations

from aijudge_authoring import TaskSpec, build_task_version, content
from aijudge_authoring.repository import substantive
from aijudge_core.ids import CourseId, UserId

AUTHOR = UserId("usr_" + "1" * 32)
COURSE = CourseId("crs_" + "1" * 32)

SPEC = TaskSpec(
    key="ex1/p1",
    statement="## 課題 ##\n\n本文",
    unit="ex1",
    knowledge_components=("cs.sdf.fundamentals.variable", "cs.sdf.fundamentals.control_flow"),
)


def _version(*, version: int) -> object:
    return build_task_version(
        SPEC,
        course_id=COURSE,
        subject_profile="cs_lang_c_intro",
        authored_by=AUTHOR,
        version=version,
    )


def test_the_same_declaration_has_the_same_content_across_versions() -> None:
    """版番号だけが違う 2 つの版は、`content()` では同じでなければならない。"""
    v1, v2 = _version(version=1), _version(version=2)

    assert v1.q_matrix  # 前提: この課題は KC を宣言している
    assert v1.q_matrix[0].task_version_id != v2.q_matrix[0].task_version_id
    assert v1.id != v2.id

    assert content(v1) == content(v2)


def test_substantive_still_tells_the_versions_apart() -> None:
    """`substantive()` は不変性の判定に使うので、版番号の違いは残す。

    `save_version` が「既存の版と ID が同じで中身が違えば拒否する」ために
    使う関数なので、`content()` と違って同一性を外してはいけない。
    """
    v1, v2 = _version(version=1), _version(version=2)
    assert substantive(v1) != substantive(v2)


def test_content_still_catches_a_real_change_in_the_knowledge_components() -> None:
    """外すのは版番号の影響だけ。KC そのものが変われば `content()` も変わる。"""
    other = SPEC.model_copy(update={"knowledge_components": ("cs.sdf.fundamentals.variable",)})
    changed = build_task_version(
        other, course_id=COURSE, subject_profile="cs_lang_c_intro", authored_by=AUTHOR, version=2
    )
    assert content(_version(version=1)) != content(changed)
