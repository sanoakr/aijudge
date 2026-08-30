"""生成された課題が採点に使えるかを確かめる門（S2、設計方針 §5）。

**ここが品質の要である。** 生成そのものより、生成物を落とす仕組みの方が難しく、
そして重要である。門を通らない課題は採点に使わない。

    課題の候補（問題文・参照解答・テストケース）
      → 門 1: 参照解答がすべてのテストケースを通る
      → 門 2: **変異させた参照解答が落ちる**
      → 教員レビュー
      → 公開

**門 2 が本質である**（ADR 0008）。門 1 だけなら、何を渡しても通るテストケース
（`exit 0` を返すだけの検査）が満点で通ってしまう。参照解答を機械的に壊して、
壊れたものが実際に落ちることまで確かめて初めて、テストケースが何かを見ている
と言える。Phase 4 の合格基準「解の一意性検証で不正な問題を 95% 以上除去」は
この門が担う。

このモジュールは**実行しない。** 変異を作る規則と、結果の読み方だけを持つ。
実際に走らせるのは app 層（サンドボックスと採点パイプラインを束ねてよい
唯一の層、ADR 0001）。分けてあるので、変異の規則は実行環境なしで試験できる。
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .difficulty import DifficultyEstimate
from .similarity import DuplicateReport
from .solvability import SolvabilityReport

# 門 2 で殺せていなければならない変異の割合。
#
# **1.0 にはしない。** 変異のなかには意味を変えないもの（到達しない分岐、
# 出力に出ない中間値）が必ず混じる。そこまで要求すると、正しい課題が
# 落ち続けて門が使われなくなる。
DEFAULT_MIN_KILL_RATIO = 0.8


class MutationKind(StrEnum):
    """参照解答の壊し方。

    **意味を変えることが目的で、壊れ方の忠実さは目的ではない。** 学習者が
    実際にしがちな誤りを再現する必要はない ── テストケースが「何かを見ている」
    ことを確かめられればよい。
    """

    # 比較の向きを変える。境界の扱いを見ているかどうかが出る。
    FLIP_COMPARISON = "flip_comparison"
    # 数値リテラルを変える。定数を直接書いた期待値しか見ていない検査が残る。
    CHANGE_NUMBER = "change_number"
    # 文字列リテラルを変える。出力の文言を見ているかどうかが出る。
    CHANGE_STRING = "change_string"
    # 文を 1 つ消す。**最も強い変異** ── 消しても通るなら、その行は
    # テストケースから見えていない。
    DROP_STATEMENT = "drop_statement"


class Mutation(BaseModel):
    """変異 1 つ。どこを、何から何に変えたか。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MutationKind
    # 1 始まりの行番号。教員に「この行を壊したら通ってしまった」と示すため。
    line: int = Field(ge=1)
    original: str
    mutated: str
    source: str = Field(min_length=1)

    @property
    def label(self) -> str:
        return f"{self.kind.value}@{self.line}"


_COMPARISONS = (
    # 長いものから試す。`<=` を `<` より先に見ないと `<` が先に当たる。
    ("<=", ">"),
    (">=", "<"),
    ("==", "!="),
    ("!=", "=="),
    ("<", ">="),
    (">", "<="),
)

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
# エスケープを含む文字列も対象にする。`"%d\n"` のような、実際のコードで
# いちばん多い形が外れていた（`\` を除いていたため）。
_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.){1,80})"')

# 消しても意味を持たない行。消して落ちるはずがないので変異にしない。
#
# **`return 0;` を含めるのは等価変異だからである。** C99 以降の `main` は
# 最後まで到達すれば 0 を返すので、この行を消しても振る舞いは変わらない ──
# どんなテストケースでも殺せない変異を作ると、門 2 が「テストケースが弱い」
# ではなく「等価変異が混じった」を測ることになる。
#
# 等価変異を完全に無くすことはできない（一般には決定不能）。だから閾値も
# 1.0 にしない。ここで落とせるのは、**よく出る形が分かっているもの**だけである。
_SKIP_DROP_RE = re.compile(r"^\s*(?:$|//|#|/\*|\*|\}|\{|else\b|return\s+0\s*;\s*$|return\s*;\s*$)")

# **どの変異の対象にもしない行。** import と `#include` は言語の宣言で、
# ここを壊すと「テストケースが何を見ているか」ではなく
# 「コンパイラが動くか」を測ることになる。`#include <stdio.h>` の `<` を
# 比較演算子として反転していた（実際にそうなっていた）。
_NEVER_MUTATE_RE = re.compile(r"^\s*(?:#\s*(?:include|define|pragma)|import\b|from\b|using\b)")


def mutate(source: str, *, limit: int = 20) -> tuple[Mutation, ...]:
    """参照解答を機械的に壊した版を作る。

    **言語を知らない。** C と Python の両方を同じ規則で扱う ── 構文木を
    持ち込むと言語ごとに実装が要り、科目を足すたびにここが増える（P1 に反する）。
    行と字面だけを見る規則でも、テストケースが何も見ていないことは十分に暴ける。

    `limit` で打ち切るのは、変異の数だけサンドボックスの実行が要るためである。
    """
    lines = source.splitlines()
    found: list[Mutation] = []

    for index, line in enumerate(lines, start=1):
        for mutation in _mutations_for(line, index, lines):
            found.append(mutation)
            if len(found) >= limit:
                return tuple(found)
    return tuple(found)


def _mutations_for(line: str, index: int, lines: list[str]) -> list[Mutation]:
    if _NEVER_MUTATE_RE.match(line):
        return []
    made: list[Mutation] = []

    for original, replacement in _COMPARISONS:
        if original in line:
            made.append(
                _one(
                    MutationKind.FLIP_COMPARISON,
                    index,
                    lines,
                    line.replace(original, replacement, 1),
                    original,
                    replacement,
                )
            )
            break

    number = _NUMBER_RE.search(line)
    if number is not None:
        value = int(number.group(1))
        made.append(
            _one(
                MutationKind.CHANGE_NUMBER,
                index,
                lines,
                line[: number.start(1)] + str(value + 1) + line[number.end(1) :],
                number.group(1),
                str(value + 1),
            )
        )

    text = _STRING_RE.search(line)
    if text is not None:
        made.append(
            _one(
                MutationKind.CHANGE_STRING,
                index,
                lines,
                line[: text.start(1)] + text.group(1) + "_" + line[text.end(1) :],
                text.group(1),
                text.group(1) + "_",
            )
        )

    if not _SKIP_DROP_RE.match(line):
        made.append(_one(MutationKind.DROP_STATEMENT, index, lines, "", line.strip(), ""))

    return made


def _one(
    kind: MutationKind,
    index: int,
    lines: list[str],
    replacement: str,
    original: str,
    mutated: str,
) -> Mutation:
    changed = list(lines)
    changed[index - 1] = replacement
    return Mutation(
        kind=kind,
        line=index,
        original=original,
        mutated=mutated,
        source="\n".join(changed) + "\n",
    )


class MutationOutcome(StrEnum):
    """変異 1 つを走らせた結果。

    **`NOT_VIABLE` を「殺した」に数えてはならない。** 関数の宣言行を消せば
    コンパイルは必ず失敗するが、それはテストケースが何かを見ている証拠には
    ならない ── 数えると、`exit 0` を返すだけの検査でも門 2 を通ってしまい、
    門 2 を置いた意味がなくなる。
    """

    # テストケースが落とした。**これだけが門 2 の証拠になる。**
    KILLED = "killed"
    # 壊したのにテストケースが通した。**教員への指示になる。**
    SURVIVED = "survived"
    # そもそも動かない（コンパイルできない等）。分母から外す。
    NOT_VIABLE = "not_viable"


class GateOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # 検査そのものが走らなかった（コンパイラが無い等）。**合格ではない。**
    NOT_RUN = "not_run"


class VerificationReport(BaseModel):
    """2 つの門の結果。**教員が読む文書でもある。**

    数字だけを返さない。どの変異が生き残ったかを名指しできないと、教員は
    テストケースをどう足せばよいか分からない（設計原則 P4 を作問にも適用する）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_passes: GateOutcome
    reference_detail: str = ""
    # **動いた変異の数**（`NOT_VIABLE` を含まない）。割合の分母になる。
    mutants_total: int = Field(default=0, ge=0)
    mutants_killed: int = Field(default=0, ge=0)
    # コンパイルできなかった変異。数えないが、多すぎれば変異の作り方が悪い
    # 合図なので捨てずに残す。
    mutants_not_viable: int = Field(default=0, ge=0)
    # 生き残った変異。**ここが教員への指示になる。**
    survivors: tuple[Mutation, ...] = ()
    min_kill_ratio: float = Field(default=DEFAULT_MIN_KILL_RATIO, ge=0.0, le=1.0)

    @property
    def kill_ratio(self) -> float | None:
        if self.mutants_total == 0:
            return None
        return self.mutants_killed / self.mutants_total

    @property
    def gate_two(self) -> GateOutcome:
        ratio = self.kill_ratio
        if ratio is None:
            # 変異が作れなかった。**合格にしない** ── 参照解答が空に近い
            # ときに起き、そのとき門 2 は何も確かめていない。
            return GateOutcome.NOT_RUN
        return GateOutcome.PASSED if ratio >= self.min_kill_ratio else GateOutcome.FAILED

    @property
    def usable(self) -> bool:
        """採点に使ってよいか。**両方の門を通ったときだけ真。**

        `NOT_RUN` は合格ではない ── 測れなかったことを合格として扱うのは
        ADR 0005 が測定について禁じたのと同じ形である。
        """
        return self.reference_passes is GateOutcome.PASSED and self.gate_two is GateOutcome.PASSED

    def summary(self) -> str:
        ratio = self.kill_ratio
        killed = (
            "—" if ratio is None else f"{self.mutants_killed}/{self.mutants_total}（{ratio:.0%}）"
        )
        lines = [
            f"門 1 参照解答が通る: {self.reference_passes.value}",
            f"門 2 変異が落ちる:   {self.gate_two.value}  {killed}",
        ]
        if self.reference_detail:
            lines.append(f"  {self.reference_detail}")
        if self.mutants_not_viable:
            lines.append(f"  動かなかった変異 {self.mutants_not_viable} 件（分母から除外）")
        for mutation in self.survivors:
            lines.append(
                f"  生き残り {mutation.label}: {mutation.original!r} → {mutation.mutated!r}"
            )
        return "\n".join(lines)


class TaskChecks(BaseModel):
    """課題版 1 つに対して走らせた検査の記録。

    **保存する。** 門はサンドボックスで実行するので数秒かかり、教員が
    レビュー画面を開くたびに走らせるわけにいかない。走らせた結果を残さないと、
    画面には「検査した」としか出せず、**何が生き残ったのかを教員に示せない**
    ── それでは承認の判断材料にならない（設計原則 P4）。

    採点結果（`GradingRun`）とは別物である。あちらは学習者の提出に対する
    判定で、こちらは課題そのものに対する検査。混ぜない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verification: VerificationReport
    # 解答可能性。門が落ちたときは走らせないので None になる。
    solvability: SolvabilityReport | None = None
    # 宣言された知識要素の**正準キー**。ID から引き直さずに済むよう、
    # 検査した時点のものを残す（KC が後から消えても教員には読める）。
    declared_kcs: tuple[str, ...] = ()
    # 既存の課題との重複。埋め込みが無ければ字面で測った結果が入る。
    duplicates: DuplicateReport | None = None
    # 似た課題の正答率から見込んだ難度。データが無ければ推定しない。
    difficulty: DifficultyEstimate | None = None
    checked_at: datetime | None = None
