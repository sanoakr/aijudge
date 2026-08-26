"""採点一致度の指標（S9）。

PoC-1 の合格基準はここで測る。実装を間違えると「合格した」と誤認するので、
手計算で検算できる値をテストに固定してある（tests/test_metrics.py）。

純関数のみ。I/O も採点の語彙も持ち込まない。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field


def _check_pairs(human: Sequence[int], machine: Sequence[int]) -> None:
    if len(human) != len(machine):
        raise ValueError(f"rating lists differ in length: {len(human)} vs {len(machine)}")
    if not human:
        raise ValueError("cannot compute agreement on an empty set")


def confusion_matrix(
    human: Sequence[int], machine: Sequence[int], levels: Sequence[int]
) -> list[list[int]]:
    """行が人間、列が機械。段階は `levels` の順に並ぶ。"""
    _check_pairs(human, machine)
    index = {level: position for position, level in enumerate(levels)}
    matrix = [[0 for _ in levels] for _ in levels]
    for h, m in zip(human, machine, strict=True):
        if h not in index or m not in index:
            raise ValueError(f"rating outside the declared levels: human={h} machine={m}")
        matrix[index[h]][index[m]] += 1
    return matrix


def exact_agreement(human: Sequence[int], machine: Sequence[int]) -> float:
    """完全一致率。κ と違い偶然の一致を割り引かないので、単独では使わない。"""
    _check_pairs(human, machine)
    return sum(1 for h, m in zip(human, machine, strict=True) if h == m) / len(human)


def cohen_kappa(human: Sequence[int], machine: Sequence[int]) -> float:
    """Cohen の κ。名義尺度の一致度。偶然の一致を割り引く。

    段階の「近さ」を考慮しないので、0 と 3 の食い違いも 0 と 1 の食い違いも
    同じ扱いになる。順序のある採点段階には QWK の方が適切だが、
    PoC-1 の合格基準が κ で書かれているので両方出す。
    """
    _check_pairs(human, machine)
    total = len(human)
    observed = exact_agreement(human, machine)

    human_counts = Counter(human)
    machine_counts = Counter(machine)
    expected = sum(
        (human_counts[level] / total) * (machine_counts[level] / total)
        for level in set(human_counts) | set(machine_counts)
    )

    if math.isclose(expected, 1.0):
        # 全員が同じ段階を付けた。偶然でも一致するので κ は定義できない。
        return 1.0 if math.isclose(observed, 1.0) else 0.0
    return (observed - expected) / (1.0 - expected)


def quadratic_weighted_kappa(
    human: Sequence[int], machine: Sequence[int], levels: Sequence[int]
) -> float:
    """二次重み付き κ。順序尺度の標準指標。

    段階が離れているほど不一致を重く数えるので、採点段階の評価に適する。
    `levels` は昇順で、実際に出現しうる段階をすべて含めること
    （出現しなかった段階も期待値の計算に効く）。
    """
    _check_pairs(human, machine)
    ordered = sorted(levels)
    if len(ordered) < 2:
        raise ValueError("quadratic weighted kappa needs at least two levels")

    size = len(ordered)
    span = (size - 1) ** 2
    weights = [[((i - j) ** 2) / span for j in range(size)] for i in range(size)]

    observed = confusion_matrix(human, machine, ordered)
    total = len(human)
    human_hist = [sum(row) for row in observed]
    machine_hist = [sum(observed[i][j] for i in range(size)) for j in range(size)]

    numerator = sum(weights[i][j] * observed[i][j] for i in range(size) for j in range(size))
    denominator = sum(
        weights[i][j] * human_hist[i] * machine_hist[j] / total
        for i in range(size)
        for j in range(size)
    )

    if math.isclose(denominator, 0.0):
        # 一方の評価者が全員に同じ段階を付けた。期待不一致が 0 なので比が取れない。
        return 1.0 if math.isclose(numerator, 0.0) else 0.0
    return 1.0 - numerator / denominator


def population_stdev(values: Sequence[float]) -> float:
    """母標準偏差。同一提出を繰り返し採点したときのばらつきに使う。"""
    if not values:
        raise ValueError("cannot compute the spread of an empty sample")
    if len(values) == 1:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


class AgreementReport(BaseModel):
    """1 観点分の一致度。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_code: str = Field(min_length=1)
    sample_size: int = Field(ge=0)
    levels: tuple[int, ...] = ()
    exact_agreement: float | None = None
    cohen_kappa: float | None = None
    quadratic_weighted_kappa: float | None = None
    confusion: tuple[tuple[int, ...], ...] = ()
    # 人間が付けた段階から機械の段階を引いた平均。正なら機械が甘い。
    mean_bias: float | None = None

    @property
    def measurable(self) -> bool:
        return self.cohen_kappa is not None


def agreement_report(
    criterion_code: str,
    human: Sequence[int],
    machine: Sequence[int],
    levels: Sequence[int],
) -> AgreementReport:
    """一致度をまとめて算出する。標本が空なら「測れない」と返す。

    標本が無いときに 0 や 1 を返さないのが要点。測れていないことと、
    一致していないことは違う。
    """
    ordered = tuple(sorted(levels))
    if not human:
        return AgreementReport(criterion_code=criterion_code, sample_size=0, levels=ordered)

    matrix = confusion_matrix(human, machine, ordered)
    return AgreementReport(
        criterion_code=criterion_code,
        sample_size=len(human),
        levels=ordered,
        exact_agreement=exact_agreement(human, machine),
        cohen_kappa=cohen_kappa(human, machine),
        quadratic_weighted_kappa=quadratic_weighted_kappa(human, machine, ordered),
        confusion=tuple(tuple(row) for row in matrix),
        mean_bias=sum(h - m for h, m in zip(human, machine, strict=True)) / len(human),
    )


def miss_rate(auto_confirmed: Sequence[bool], human_changed: Sequence[bool]) -> float | None:
    """見逃し率 — 自動確定したのに人間の修正が必要だった割合。

    PoC-1 で最も重い指標。ここが高いと、レビューを省いた提出の中に
    誤った成績が紛れていることになる。自動確定が 1 件も無ければ測れない。
    """
    _check_pairs([int(x) for x in auto_confirmed], [int(x) for x in human_changed])
    automatic = [
        changed for auto, changed in zip(auto_confirmed, human_changed, strict=True) if auto
    ]
    if not automatic:
        return None
    return sum(1 for changed in automatic if changed) / len(automatic)


def review_rate(auto_confirmed: Sequence[bool]) -> float:
    """レビュー行きの割合。高すぎれば教員の負担が減らない。"""
    if not auto_confirmed:
        raise ValueError("cannot compute the review rate of an empty set")
    return sum(1 for auto in auto_confirmed if not auto) / len(auto_confirmed)
