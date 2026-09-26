"""採点は、実際に効いた科目プロファイルを記録する（#407）。

名前だけでは、コースの上書きや評価器の設定を学期中に変えたあとで、どの設定で
採点したかを言えない（P8）。
"""

from __future__ import annotations

from aijudge_grading import SubjectProfile
from aijudge_grading.pipeline import profile_hash


def test_the_same_profile_hashes_the_same() -> None:
    a = SubjectProfile(name="p", evaluator_options={"x": {"a": 1, "b": 2}})
    b = SubjectProfile(name="p", evaluator_options={"x": {"b": 2, "a": 1}})
    assert profile_hash(a) == profile_hash(b)
    assert profile_hash(a).startswith("sha256:")


def test_an_override_changes_the_hash() -> None:
    base = SubjectProfile(name="p")
    tightened = base.model_copy(
        update={"review_policy": base.review_policy.model_copy(update={"confidence_below": 0.9})}
    )
    assert profile_hash(base) != profile_hash(tightened)
