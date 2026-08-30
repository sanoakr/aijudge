"""別のルーブリックで付けた採点を、その年度の人の採点と比べる。**採点はしない。**

ある年度の採点例から起こしたルーブリックを、別の年度の提出に当てて
汎化を測るための道具（転移テスト）。

**尺度が違うものを QWK でそのまま比べない。** 2025 年度のルーブリックは
23 点満点、2023 年度の教員採点は 50 段階で、観点も違う。別々の物差しなので、
生の QWK は「目盛りが違う」ことを測ってしまう。

だから 2 段構えで見る。

  1. **順位**（ρ・τ・r）── 尺度に依らない。**これが転移の本体である。**
     ルーブリックが「誰が上か」を移せているかを測る。
  2. **1 次式で人の尺度に写したあとの QWK** ── 1 件抜き交差検証で写す。
     順位が合っている範囲で、目盛りを合わせればどこまで届くかの上限。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import rubric

from aijudge_analytics.metrics import quadratic_weighted_kappa


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = average
        i = j + 1
    return out


def pearson(xs, ys) -> float | None:
    if len(xs) < 3 or statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / len(xs)
    return cov / (statistics.pstdev(xs) * statistics.pstdev(ys))


def spearman(xs, ys) -> float | None:
    return pearson(_ranks(list(xs)), _ranks(list(ys)))


def kendall(xs, ys) -> float | None:
    con = dis = 0
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            a = (xs[i] - xs[j]) * (ys[i] - ys[j])
            con += a > 0
            dis += a < 0
    return None if con + dis == 0 else (con - dis) / (con + dis)


def calibrated(source: list[int], target: list[int], top: int) -> list[int]:
    """1 件抜き交差検証で人の尺度に写す。

    **同じ標本で当てはめて測ると必ず良くなる**ので、各件の変換は
    その件を除いた残りから決める。
    """
    out = []
    for i in range(len(source)):
        xs = [source[j] for j in range(len(source)) if j != i]
        ys = [target[j] for j in range(len(target)) if j != i]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        var = sum((x - mx) ** 2 for x in xs)
        a = 0.0 if var == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
        out.append(max(0, min(top, round(a * source[i] + (my - a * mx)))))
    return out


def machine_totals(name: str) -> tuple[dict[str, int], int]:
    """採点結果の合計。**採点に使ったルーブリックの尺度のまま返す。**

    合計を出せなかった件数も返す。**出せない理由はたいてい観点の食い違い**
    ── その採点は別のルーブリックで走っており、`AIJUDGE_EVAL_RUBRIC` の
    指定が要る。黙って落とすと行ごと消えて、原因が見えなくなる。
    """
    runs = json.loads((rubric.RUNS / f"{name}.json").read_text("utf-8"))
    out = {}
    dropped = 0
    for row in runs:
        levels = {code: score["level"] for code, score in row["scores"].items()}
        value = rubric.total(levels)
        if value is None:
            dropped += 1
            continue
        out[row["login"]] = value
    return out, dropped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", help="`実行名` か `実行名:表示名`")
    args = parser.parse_args()

    index = json.loads(rubric.INDEX.read_text("utf-8"))
    human_levels = {r["login"]: r["human"] for r in index if r["human"]}
    # 人の採点は**その年度のルーブリック**で合計する（機械側とは尺度が違う）。
    source = sys.modules["rubric_" + rubric.DATASET]
    human = {}
    for login, levels in human_levels.items():
        if all(c in levels for c in source.POINTS):
            human[login] = sum(levels[c] * source.TOTAL_WEIGHT[c] for c in source.POINTS)
    top = source.TOTAL_MAX

    print(f"# 転移テスト — {rubric.DATASET} 年度の提出、照合先は同年度の人の採点\n")
    print(f"採点に使ったルーブリック: **{rubric.RUBRIC}**（0〜{rubric.TOTAL_MAX} 段階）")
    print(f"照合先: {rubric.DATASET} 年度の採点表（0〜{top} 段階）\n")
    print("**尺度が違うので生の QWK は比べない。** 順位（ρ・τ・r）が転移の本体で、")
    print("QWK は 1 次式で人の尺度に写したあと（1 件抜き交差検証）の値である。\n")
    notes: list[str] = []
    print("| 実行 | n | ρ（順位） | τ | r | 較正後 QWK |")
    print("|---|--:|--:|--:|--:|--:|")
    for spec in args.runs:
        name, _, label = spec.partition(":")
        label = label or name
        try:
            machine, dropped = machine_totals(name)
        except FileNotFoundError:
            print(f"| {label} | — | （{name}.json が無い） | | | |")
            continue
        if not machine:
            print(
                f"| {label} | — | （{rubric.RUBRIC} の観点で合計が出せない ── "
                f"{dropped} 件すべてで観点が欠けている。この採点を走らせた"
                f"ルーブリックを `AIJUDGE_EVAL_RUBRIC` で指定すること） | | | |"
            )
            continue
        shared = sorted(set(human) & set(machine))
        if len(shared) < 4:
            print(f"| {label} | {len(shared)} | （照合できる件数が少なすぎる） | | | |")
            continue
        if dropped:
            # 表の行にはしない（1 つの実行が 2 行になると読めなくなる）。
            notes.append(f"- `{label}`: {dropped} 件は観点が欠けていて合計を出せない")
        h = [human[i] for i in shared]
        m = [machine[i] for i in shared]
        fixed = calibrated(m, h, top)
        print(
            f"| {label} | {len(shared)} | {spearman(m, h):+.3f} | {kendall(m, h):+.3f}"
            f" | {pearson(m, h):+.3f}"
            f" | {quadratic_weighted_kappa(h, fixed, range(top + 1)):+.3f} |"
        )
    if notes:
        print()
        for note in notes:
            print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
