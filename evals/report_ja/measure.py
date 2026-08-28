"""採点結果を教員の実採点と突き合わせる。**採点はしない**（ADR 0007）。

指標の計算は `packages/analytics` にやらせる。ここで別の κ を書くと、
本番の測定と違う数字を見ることになる。

段階は教員の配点と 1 対 1 にしてあるので、写し替えずにそのまま比べられる。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "dev/aijudge"))
sys.path.insert(0, str(Path(__file__).parent))

import rubric

from aijudge_analytics.metrics import (
    cohen_kappa,
    exact_agreement,
    quadratic_weighted_kappa,
)

# gates.yaml の基準（Phase 1）。
GATE_KAPPA = 0.65
GATE_QWK = 0.60
GATE_REVIEW_RATE = 0.30


def pearson(xs, ys) -> float | None:
    if len(xs) < 2 or statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / len(xs)
    return cov / (statistics.pstdev(xs) * statistics.pstdev(ys))


def mark(value: float | None, gate: float) -> str:
    if value is None:
        return "—"
    return "✓" if value >= gate else "✗"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="run1")
    args = parser.parse_args()

    index = {r["login"]: r for r in json.loads(rubric.INDEX.read_text("utf-8"))}
    runs = json.loads((rubric.RUNS / f"{args.name}.json").read_text("utf-8"))

    print(f"# 測定 — [{rubric.DATASET}] {args.name}（提出 {len(runs)} 件）\n")

    # --- 観点ごとの一致度 ------------------------------------------------
    print("観点              点  n   完全一致   κ         QWK       平均差")
    print("-" * 72)
    for spec in rubric.CRITERIA:
        code = spec["code"]
        pairs = [
            (index[r["login"]]["human"][code], r["scores"][code]["level"])
            for r in runs
            if code in r["scores"] and index[r["login"]]["human"]
        ]
        if not pairs:
            print(f"{spec['title']:16} {rubric.POINTS[code]:2}  0   （採点できた提出が無い）")
            continue
        human = [p[0] for p in pairs]
        machine = [p[1] for p in pairs]
        kappa = cohen_kappa(human, machine)
        # 段階は 0 から配点まで。出現しなかった段階も期待値に効くので
        # 全部渡す。
        qwk = quadratic_weighted_kappa(
            human, machine, range(rubric.MAX_LEVEL[code] + 1)
        )
        bias = statistics.fmean(m - h for h, m in pairs)
        print(
            f"{spec['title']:16} {rubric.POINTS[code]:2} {len(pairs):2}"
            f"   {exact_agreement(human, machine):6.1%}"
            f"   {kappa:+.3f} {mark(kappa, GATE_KAPPA)}"
            f"  {qwk:+.3f} {mark(qwk, GATE_QWK)}"
            f"  {bias:+.2f}"
        )

    # --- 合計点の一致 ----------------------------------------------------
    print(f"\n## 合計点（0〜{rubric.TOTAL_MAX} 段階）\n")
    rows = []
    for r in runs:
        human = index[r["login"]]["human"]
        if human is None or r["unscored"]:
            continue
        m_total = rubric.total({c: v["level"] for c, v in r["scores"].items()})
        h_total = rubric.total(human)
        if m_total is None or h_total is None:
            continue
        rows.append((r["login"], h_total, m_total, r["routing"]))
    if rows:
        h = [x[1] for x in rows]
        m = [x[2] for x in rows]
        diffs = [b - a for a, b in zip(h, m, strict=True)]
        corr = pearson(h, m)
        print(f"  全観点が採点できた提出: {len(rows)} / {len(runs)}")
        print(f"  相関 r = {corr:+.3f}" if corr is not None else "  相関 = —")
        print(
            f"  QWK（0〜{rubric.TOTAL_MAX} 段階） = "
            f"{quadratic_weighted_kappa(h, m, range(rubric.TOTAL_MAX + 1)):+.3f}"
        )
        print(
            f"  平均差 {statistics.fmean(diffs):+.2f} 点 /"
            f" 平均絶対差 {statistics.fmean(abs(d) for d in diffs):.2f} 点 /"
            f" 最大 {max(diffs, key=abs):+d} 点"
        )
        print("\n  学生       教員  AI   差   振り分け")
        for login, ht, mt, routing in sorted(rows, key=lambda x: x[1] - x[2]):
            print(f"  {login:9} {ht:4} {mt:4} {mt-ht:+4}   {routing}")

    # --- 振り分けと未採点 -------------------------------------------------
    review = sum(1 for r in runs if r["routing"] != "auto")
    print("\n## 振り分け\n")
    print(
        f"  レビュー行き {review}/{len(runs)} = {review/len(runs):.0%}"
        f"  {mark(GATE_REVIEW_RATE - review/len(runs), 0.0)}（基準 30% 以下）"
    )
    unscored = [(r["login"], r["unscored"]) for r in runs if r["unscored"]]
    if unscored:
        print(f"  未採点の観点がある提出: {len(unscored)} 件")
        for login, codes in unscored:
            print(f"    {login}: {'・'.join(codes)}")
    failures = [
        (r["login"], e["id"], e["error"])
        for r in runs
        for e in r["evaluators"]
        if e["status"] not in ("ok", "skipped")
    ]
    if failures:
        print(f"\n  評価器の失敗 {len(failures)} 件")
        for login, evaluator, error in failures[:10]:
            print(f"    {login} {evaluator}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
