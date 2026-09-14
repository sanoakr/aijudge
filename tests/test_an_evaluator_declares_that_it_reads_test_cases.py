"""入出力セットを読む評価器は、そう宣言していること（#300）。

教員コンソールは**この宣言だけ**を見て、課題の編集画面に入出力セット
（テストケース）の欄を出すかどうかを決める（`_test_driven_criteria`）。
画面が評価器名の表を持たないのはそのためで、宣言が抜けた評価器を割り当てた
課題では**欄そのものが出ない** ── 何を走らせているのかを確かめる手段が
無くなる。宣言は書き忘れても採点は動くので、ここで落とす。

`import-linter` は見られない（評価器は画面から import されない）ので、
`tests/test_evals_stay_out_of_production.py` と同じくソースを読んで確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATORS = REPO_ROOT / "evaluators"

#: 入出力セットを読んでいる印。`EvaluationRequest.test_cases` の読み出し。
READS = re.compile(r"\brequest\.test_cases\b")
DECLARES = re.compile(r"^\s*uses_test_cases\s*=\s*True\s*$", re.M)


def _sources() -> list[Path]:
    found = sorted(EVALUATORS.glob("*/src/*/__init__.py"))
    assert found, "評価器のソースを走査できていない"
    return found


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.parents[1].name)
def test_an_evaluator_that_reads_test_cases_declares_it(source: Path) -> None:
    text = source.read_text(encoding="utf-8")
    if not READS.search(text):
        # 読まない評価器は宣言しない（既定は「読まない」）。**書いてもいけない**
        # ── 読まないのに宣言すると、空の欄が出続ける課題ができる。
        assert not DECLARES.search(text), f"{source} は読まないのに宣言している"
        return
    assert DECLARES.search(text), (
        f"{source} は入出力セットを読むのに uses_test_cases = True を宣言していない"
    )


def test_the_installed_evaluators_agree_with_their_sources() -> None:
    """宣言が**実際に登録される物**に載っていること。

    ソースにあってもクラスの外に書けば属性にならない。登録済みの評価器を
    引いて確かめる。
    """
    from aijudge_grading import EvaluatorRegistry, reads_test_cases

    registry = EvaluatorRegistry().load_installed()
    declared = {name for name in registry.ids() if reads_test_cases(registry.get(name))}
    assert "code_test_runner" in declared
    # 提出の遵守は決定論的だが入出力セットを持たない。**一緒にしない。**
    assert "submission_compliance" not in declared
