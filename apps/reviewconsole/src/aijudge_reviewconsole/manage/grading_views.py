"""評価器・観点の表示（段階 4-7）。

コース設定と課題の画面が**同じ規則で見せる**ために 1 か所に置く ── 片方だけ直る形を避ける
（地図 `docs/design/manage-split-map.md` の `grading_views.py`）。
"""

from __future__ import annotations


def _evaluator_rows(registry, kind) -> list[dict[str, str]]:
    """評価器の名前と 1 行説明。

    **説明は評価器が持つ**（クラスの docstring の 1 行目）。画面が名前ごとの
    表を持つと、評価器を足したときに説明だけ抜ける。
    """
    rows = []
    for name in sorted(registry.ids_of_kind(kind)):
        doc = (registry.get(name).__doc__ or "").strip()
        rows.append({"name": name, "about": doc.splitlines()[0] if doc else ""})
    return rows


def _default_rubric_criteria():
    """組み込みの既定（正しさ＋読みやすさ）を宣言の形で返す。

    未設定のコースでも**いま何が使われているか**を画面に出すため。空欄を
    見せると、観点が無いのか既定なのかが分からない。
    """
    from aijudge_authoring.importers.sharif_judge import (
        correctness_criterion,
        readability_criterion,
    )

    correctness = correctness_criterion()
    return (
        correctness.model_copy(update={"weight": 0.7}),
        readability_criterion(0.3),
    )
