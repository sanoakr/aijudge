"""3 者（CSV・qwen3.8:27b・gemma4:e4b）の相互一致度。**採点はしない。**

**CSV の採点も LLM が付けたものである。** したがってこれは「正解に対する
精度」ではなく、**独立した 3 人の採点者がどれだけ揃うか**の測定である。

この区別は結論を変える。正解として扱えば「AI が教員にどれだけ近いか」だが、
3 者比較では「そもそもこのルーブリックで採点者が揃うのか」を測ることになる。
どの観点でも 3 者が揃わないなら、問題はモデルではなくルーブリックの側にある。

比較は**同じルーブリック・同じ設定の実行どうし**でしか意味を持たないので、
既定では samples=2 の 2 本（teacher-rubric = gemma / qwen27b = qwen）を
並べる。設定を変えた実行は `--extra` で足す。
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import rubric

from aijudge_analytics.metrics import (
    cohen_kappa,
    exact_agreement,
    quadratic_weighted_kappa,
)


def load_csv_rater(index: dict) -> dict[str, dict[str, int]]:
    """CSV の採点を、実行結果と同じ形（学生 → 観点 → 段階）にする。"""
    out = {}
    for login, row in index.items():
        if row["human"] is None:
            continue
        out[login] = dict(row["human"])
    return out


def load_run(name: str) -> dict[str, dict[str, int]]:
    runs = json.loads((rubric.RUNS / f"{name}.json").read_text("utf-8"))
    return {r["login"]: {code: s["level"] for code, s in r["scores"].items()} for r in runs}


def pairwise(a: dict, b: dict, code: str) -> tuple[list[int], list[int]]:
    """両方が段階を付けた提出だけを対にする。

    片方が未採点のものを 0 で埋めない。埋めると「採点できなかった」が
    「0 点を付けた」として一致度に入る。
    """
    logins = sorted(set(a) & set(b))
    pairs = [(a[x][code], b[x][code]) for x in logins if code in a[x] and code in b[x]]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def totals(rater: dict) -> dict[str, int]:
    """全観点が揃っている提出だけの合計点（23 点尺度）。"""
    out = {}
    for login, levels in rater.items():
        value = rubric.total(levels)
        if value is not None:
            out[login] = value
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raters",
        nargs="+",
        default=["csv", "teacher-rubric:gemma4:e4b", "qwen27b:qwen3.8:27b"],
        help="`実行名:表示名`。csv は CSV の採点。",
    )
    args = parser.parse_args()

    index = {r["login"]: r for r in json.loads(rubric.INDEX.read_text("utf-8"))}
    raters: dict[str, dict] = {}
    for spec in args.raters:
        name, _, label = spec.partition(":")
        label = label or name
        raters[label] = load_csv_rater(index) if name == "csv" else load_run(name)

    names = list(raters)
    print("# 採点者どうしの一致度\n")
    print(f"採点者: {' / '.join(names)}")
    print("**CSV の採点も LLM である。** これは精度ではなく採点者間の一致度。\n")

    for spec in rubric.CRITERIA:
        code = spec["code"]
        print(f"## {spec['title']}\n")
        print("  組                              n   完全一致   κ        QWK")
        for x, y in itertools.combinations(names, 2):
            a, b = pairwise(raters[x], raters[y], code)
            if len(a) < 2:
                print(f"  {x} ↔ {y}: 対が足りない")
                continue
            levels = range(rubric.MAX_LEVEL[code] + 1)
            print(
                f"  {x + ' ↔ ' + y:30} {len(a):2}"
                f"   {exact_agreement(a, b):6.1%}"
                f"   {cohen_kappa(a, b):+.3f}"
                f"   {quadratic_weighted_kappa(a, b, levels):+.3f}"
            )
        # 3 者がそれぞれどこに寄っているか。κ が低いとき、割れているのか
        # 一方が偏っているのかはこれを見ないと分からない。
        spread = []
        for name in names:
            vals = [lv[code] for lv in raters[name].values() if code in lv]
            spread.append(
                f"{name} 平均 {statistics.fmean(vals):.2f}（σ {statistics.pstdev(vals):.2f}）"
            )
        print(f"  {' / '.join(spread)}\n")

    print("## 合計点（23 点尺度）\n")
    tot = {name: totals(rater) for name, rater in raters.items()}
    for name in names:
        vals = list(tot[name].values())
        print(
            f"  {name:22} n={len(vals):2}  平均 {statistics.fmean(vals):5.2f}"
            f"  σ {statistics.pstdev(vals):5.2f}"
            f"  範囲 {min(vals)}–{max(vals)}"
        )
    print()
    # **相関と QWK を並べて見る。** 順位は合っているのに尺度が縮んでいる
    # 場合、相関は高いまま QWK だけが落ちる。両方見ないと、採点者が
    # 「違う順に並べている」のか「同じ順に狭く並べている」のか分からない。
    print("  組                              n   r        QWK      平均差   平均絶対差")
    for x, y in itertools.combinations(names, 2):
        logins = sorted(set(tot[x]) & set(tot[y]))
        a = [tot[x][i] for i in logins]
        b = [tot[y][i] for i in logins]
        if len(a) < 2:
            continue
        diffs = [q - p for p, q in zip(a, b, strict=True)]
        mx, my = statistics.fmean(a), statistics.fmean(b)
        cov = sum((p - mx) * (q - my) for p, q in zip(a, b, strict=True)) / len(a)
        r = cov / (statistics.pstdev(a) * statistics.pstdev(b))
        print(
            f"  {x + ' ↔ ' + y:30} {len(a):2}"
            f"   {r:+.3f}"
            f"   {quadratic_weighted_kappa(a, b, range(rubric.TOTAL_MAX + 1)):+.3f}"
            f"   {statistics.fmean(diffs):+6.2f}"
            f"   {statistics.fmean(abs(d) for d in diffs):6.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
