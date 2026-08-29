"""生成された課題を落とす門の規則を固定する（S2、ADR 0008）。

固定したいのは 4 つ。

宣言は壊さない   `#include` や `import` を変異させない。壊すと、測っているのが
                 「テストケースが何を見ているか」ではなく「コンパイラが動くか」になる。
動かない変異は数えない 関数の宣言行を消せばコンパイルは必ず失敗する。それを
                 「殺した」に数えると、`exit 0` を返すだけの検査でも門 2 を通る。
測れないは合格でない `NOT_RUN` を `usable` にしない（ADR 0005 と同じ形）。
生き残りを名指しする 教員がテストケースをどう足せばよいか分かるように。
"""

from __future__ import annotations

from aijudge_authoring import (
    GateOutcome,
    MutationKind,
    VerificationReport,
    mutate,
)

C_SOURCE = """#include <stdio.h>
int main(void) {
    int total = 0;
    if (total < 10) {
        printf("small\\n");
    }
    return 0;
}
"""

PY_SOURCE = """import sys

def main() -> None:
    values = [int(x) for x in sys.stdin]
    if len(values) > 2:
        print("many")
"""


# -- 変異 -------------------------------------------------------------------


def test_language_declarations_are_never_mutated() -> None:
    """`#include <stdio.h>` の `<` は比較演算子ではない。

    ここを反転させると必ずコンパイルが落ち、その変異は「殺した」に見える。
    門 2 が甘くなる方向の誤りなので、実際にそうなっていたものを塞いである。
    """
    for source in (C_SOURCE, PY_SOURCE):
        first_line = source.splitlines()[0]
        assert all(m.line != 1 for m in mutate(source)), first_line


def test_every_mutation_changes_the_source() -> None:
    for mutation in mutate(C_SOURCE):
        assert mutation.source != C_SOURCE
        assert mutation.source.splitlines()[mutation.line - 1] != C_SOURCE.splitlines()[
            mutation.line - 1
        ]


def test_the_four_kinds_are_all_reachable() -> None:
    kinds = {m.kind for m in mutate(C_SOURCE, limit=50)}
    assert kinds == set(MutationKind)


def test_the_limit_is_honoured() -> None:
    """変異 1 つにつきサンドボックスの実行が要るので、数は制御できないと困る。"""
    assert len(mutate(C_SOURCE, limit=3)) == 3


def test_a_source_with_nothing_to_change_yields_nothing() -> None:
    assert mutate("#include <stdio.h>\n") == ()


# -- 判定 -------------------------------------------------------------------


def _report(**kwargs) -> VerificationReport:
    base = {
        "reference_passes": GateOutcome.PASSED,
        "mutants_total": 10,
        "mutants_killed": 10,
    }
    return VerificationReport(**(base | kwargs))


def test_both_gates_must_pass() -> None:
    assert _report().usable
    assert not _report(reference_passes=GateOutcome.FAILED).usable
    assert not _report(mutants_killed=1).usable


def test_not_run_is_not_a_pass() -> None:
    """**測れなかったことを合格として扱わない。**

    ADR 0005 が測定について禁じた形と同じ。参照解答が空に近くて変異が
    作れなかったとき、門 2 は何も確かめていない。
    """
    assert not _report(reference_passes=GateOutcome.NOT_RUN).usable

    empty = _report(mutants_total=0, mutants_killed=0)
    assert empty.gate_two is GateOutcome.NOT_RUN
    assert not empty.usable
    assert empty.kill_ratio is None


def test_mutants_that_cannot_run_stay_out_of_the_denominator() -> None:
    """コンパイルできない変異は、テストケースが何かを見ている証拠にならない。"""
    report = _report(mutants_total=4, mutants_killed=4, mutants_not_viable=6)
    assert report.kill_ratio == 1.0
    assert report.usable


def test_the_threshold_is_not_one() -> None:
    """意味を変えない変異は必ず混じる。1.0 を要求すると門が使われなくなる。"""
    assert _report(mutants_total=10, mutants_killed=8).usable


def test_the_summary_names_the_survivors() -> None:
    """教員が読む文書でもある。数字だけでは何を足せばよいか分からない。"""
    survivor = mutate(C_SOURCE)[0]
    text = _report(mutants_killed=8, survivors=(survivor,)).summary()
    assert survivor.label in text
    assert "生き残り" in text


def test_a_known_equivalent_mutant_is_not_created() -> None:
    """`return 0;` を消しても C99 以降の `main` は 0 を返す。

    **どんなテストケースでも殺せない変異**なので作らない。作ると、門 2 が
    「テストケースが弱い」ではなく「等価変異が混じった」を測ることになる
    （実際に、まともな解答が 67% で門 2 に落ちた）。
    """
    source = "int main(void) {\n    printf(\"x\");\n    return 0;\n}\n"
    dropped = [m for m in mutate(source) if m.kind is MutationKind.DROP_STATEMENT]
    assert all("return 0" not in m.original for m in dropped), dropped
