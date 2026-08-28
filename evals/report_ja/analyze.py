"""5 者の採点を細かく突き合わせ、報告書を 1 本の Markdown として書き出す。

**採点はしない**（ADR 0007）。読むのは `runs/*.json` と `index.json` だけ。

`agreement.py` は対ごとの一致度を出すだけだった。ここではその先を見る。

  - 不一致を**偏り・尺度・順位**の 3 つに分解する
  - **較正で直るのか**を検定する（1 次式で写して QWK が上がるか。
    同じ 19 件で当てはめると必ず上がるので、**1 件抜き交差検証**で見る）
  - 採点者を 5 人まとめて扱う（Krippendorff の α）
  - **10 点上限の判定**という、実際に成績を左右する 1 つの決定だけを取り出す
  - 各採点者の点差が**どの観点から来ているか**を分解する
  - 4 モデルの中央値（アンサンブル）が単独より良いか

出力は学籍番号を含むので、repository ではなく設計検討ディレクトリに置く。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
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

HERE = Path(__file__).parent
CODES = [c["code"] for c in rubric.CRITERIA]
TITLES = {c["code"]: c["title"] for c in rubric.CRITERIA}

# 既定の採点者。`実行名:表示名`。
DEFAULT_RATERS = [
    "csv:CSV",
    "final:gemma4",
    "qwen27b:qwen3.8",
    "sonnet5:Sonnet5",
    "opus5:Opus5",
]


# --------------------------------------------------------------------------
# 読み込み
# --------------------------------------------------------------------------


def load_raters(specs: list[str]) -> tuple[dict[str, dict], dict]:
    index = {r["login"]: r for r in json.loads((HERE / "index.json").read_text("utf-8"))}
    raters: dict[str, dict[str, dict[str, int]]] = {}
    for spec in specs:
        name, _, label = spec.partition(":")
        label = label or name
        if name == "csv":
            raters[label] = {
                login: {c: row["human"][rubric.HUMAN_COLUMN[c]] for c in CODES}
                for login, row in index.items()
                if row["human"] is not None
            }
            continue
        runs = json.loads((HERE / "runs" / f"{name}.json").read_text("utf-8"))
        raters[label] = {
            r["login"]: {c: s["level"] for c, s in r["scores"].items()} for r in runs
        }
    return raters, index


def total(levels: dict[str, int]) -> int | None:
    """全観点そろっているときだけ合計を出す。**欠けを 0 で埋めない。**"""
    if any(c not in levels for c in CODES):
        return None
    return sum(levels[c] for c in CODES)


def totals(rater: dict) -> dict[str, int]:
    out = {}
    for login, levels in rater.items():
        value = total(levels)
        if value is not None:
            out[login] = value
    return out


# --------------------------------------------------------------------------
# 統計
# --------------------------------------------------------------------------


def pearson(xs, ys) -> float | None:
    if len(xs) < 3 or statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / len(xs)
    return cov / (statistics.pstdev(xs) * statistics.pstdev(ys))


def _ranks(values: list[float]) -> list[float]:
    """同順位は平均順位にする。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def spearman(xs, ys) -> float | None:
    return pearson(_ranks(list(xs)), _ranks(list(ys)))


def kendall_tau(xs, ys) -> float | None:
    """順位の入れ替わりの割合。同点は分母から外す（tau-b ではなく素朴な形）。"""
    n = len(xs)
    if n < 3:
        return None
    concordant = discordant = 0
    for i, j in itertools.combinations(range(n), 2):
        a = (xs[i] - xs[j]) * (ys[i] - ys[j])
        if a > 0:
            concordant += 1
        elif a < 0:
            discordant += 1
    if concordant + discordant == 0:
        return None
    return (concordant - discordant) / (concordant + discordant)


def krippendorff_alpha_ordinal(units: list[list[int | None]], levels: list[int]) -> float | None:
    """順序尺度の Krippendorff の α。

    **2 人用の κ を人数分平均したものではない。** 欠測を持つ複数採点者を
    そのまま扱えるのが利点で、「5 人はどれだけ揃っているか」を 1 つの数で
    言えるのはこれ。

    units[u] は提出 u に対する各採点者の段階（採点していなければ None）。
    """
    index = {value: i for i, value in enumerate(levels)}
    size = len(levels)
    coincidence = [[0.0] * size for _ in range(size)]
    for unit in units:
        present = [v for v in unit if v is not None]
        if len(present) < 2:
            # 1 人しか付けていない提出は対を作れない。**捨てる。**
            continue
        weight = len(present) - 1
        for a, b in itertools.permutations(present, 2):
            coincidence[index[a]][index[b]] += 1.0 / weight
    marginal = [sum(row) for row in coincidence]
    n = sum(marginal)
    if n < 2:
        return None

    def delta2(c: int, d: int) -> float:
        low, high = (c, d) if c <= d else (d, c)
        inner = sum(marginal[g] for g in range(low, high + 1))
        return (inner - (marginal[c] + marginal[d]) / 2) ** 2

    observed = sum(
        coincidence[c][d] * delta2(c, d) for c in range(size) for d in range(size)
    )
    expected = 0.0
    for c in range(size):
        for d in range(size):
            pairs = (
                marginal[c] * marginal[d]
                if c != d
                else marginal[c] * (marginal[c] - 1)
            )
            expected += pairs / (n - 1) * delta2(c, d)
    if math.isclose(expected, 0.0):
        return None
    return 1.0 - observed / expected


def affine_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """y ≈ a·x + b の最小二乗解。"""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if math.isclose(var, 0.0):
        return 0.0, my
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
    return a, my - a * mx


def recalibrated_loo(source: list[int], target: list[int], top: int) -> list[int]:
    """1 件抜き交差検証で較正する。

    **同じ 19 件で当てはめて測ると必ず良くなる。** 較正で直るかを見たいので、
    各件の変換は**その件を除いた 18 件**から決める。
    """
    out = []
    for i in range(len(source)):
        rest_x = [source[j] for j in range(len(source)) if j != i]
        rest_y = [target[j] for j in range(len(target)) if j != i]
        a, b = affine_fit(rest_x, rest_y)
        value = round(a * source[i] + b)
        out.append(max(0, min(top, int(value))))
    return out


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------


def fmt(value: float | None, digits: int = 3, sign: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raters", nargs="+", default=DEFAULT_RATERS)
    parser.add_argument(
        "--out",
        default=None,
        help="書き出し先。学籍番号を含むので repository の外に置くこと",
    )
    args = parser.parse_args()

    raters, _index = load_raters(args.raters)
    names = list(raters)
    logins = sorted({login for r in raters.values() for login in r})
    tot = {name: totals(rater) for name, rater in raters.items()}
    out: list[str] = []
    w = out.append

    w("# レポート採点 — 5 者のモデル間比較")
    w("")
    w("ネットワーク及び演習 2025 年度・HTTP サーバ性能評価レポート 19 件。")
    w("**照合先の `network2025-report.csv` も LLM が付けた採点である**")
    w("（教員の正解ではない）。したがって以下は精度ではなく**採点者間の一致度**で、")
    w("設計方針 §9.2 Phase 3 の合格基準（教員採点との QWK ≥ 0.60）の判定には使えない。")
    w("判定には教員が AI を見ずに付けた段階（`BlindMark`、ADR 0005）が要る。")
    w("")
    w("生成: `uv run python evals/report_ja/analyze.py`")
    w("")
    w("---")
    w("")

    # ---- 1. 採点者 -------------------------------------------------------
    w("## 1. 採点者と、条件の違い")
    w("")
    w("| 採点者 | 経路 | 観点の呼び方 | 自己一貫性 | 制約デコード | 本文 |")
    w("|---|---|---|---|---|---|")
    w("| CSV | 不明（LLM） | 不明 | 不明 | 不明 | 不明 |")
    w("| gemma4:e4b | ゲートウェイ | 1 観点ごとに 1 回 | 3 本 | あり | 原文 |")
    w("| qwen3.8:27b-mlx | ゲートウェイ | 1 観点ごとに 1 回 | 2 本 | あり | 原文 |")
    sub = "サブエージェント | **6 観点まとめて 1 回** | **1 本** | **なし** | **匿名化**"
    w(f"| Sonnet 5 | {sub} |")
    w(f"| Opus 5 | {sub} |")
    w("")
    w("**同条件ではない。** Sonnet 5 / Opus 5 は API の資格情報が無いため")
    w("サブエージェント経由で、観点をまとめて 1 回だけ聞いている。これは")
    w("設計方針 §04 step 3 が禁じた形（観点間の引きずりが入る）で、")
    w("**この 2 つが互いによく一致することには、構成の共通性も寄与しうる。**")
    w("学外モデルには氏名・学籍番号を伏せて渡した（設計原則 P7）。")
    w("")
    w("また `qwen3.8` は 2 件で、`gemma4` の旧設定は 1 件で構造化出力に失敗して")
    w("いる。**未採点は 0 点で埋めず、対から外している。**")
    w("")

    # ---- 2. 生データ -----------------------------------------------------
    w("## 2. 生データ（観点別の段階）")
    w("")
    w("配点は 体裁 4 / 実験先 3 / 条件 2 / 独自性 8 / 考察 4 / 結果 2 = 23 点。")
    w("段階は配点と 1 対 1（丸めていない）。`—` は未採点。")
    w("")
    columns = " | ".join(TITLES[c].split("（")[0] for c in CODES)
    header = f"| 学生 | 採点者 | {columns} | 合計 |"
    w(header)
    w("|---" * (len(CODES) + 3) + "|")
    for login in logins:
        for name in names:
            levels = raters[name].get(login)
            if levels is None:
                continue
            cells = " | ".join(
                str(levels[c]) if c in levels else "—" for c in CODES
            )
            value = total(levels)
            w(f"| {login} | {name} | {cells} | {value if value is not None else '—'} |")
    w("")

    # ---- 3. 採点者ごとの分布 ---------------------------------------------
    w("## 3. 採点者ごとの分布")
    w("")
    w("### 3.1 合計点")
    w("")
    w("| 採点者 | n | 平均 | σ | 最小 | 最大 | 範囲の幅 |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for name in names:
        values = list(tot[name].values())
        w(
            f"| {name} | {len(values)} | {statistics.fmean(values):.2f}"
            f" | {statistics.pstdev(values):.2f} | {min(values)} | {max(values)}"
            f" | {max(values) - min(values)} |"
        )
    w("")
    w("**尺度を使い切っているのは CSV だけである。** 他はどれも狭い帯に詰まる。")
    w("系統の違う 4 モデルが同じ向きに詰まる以上、これは個々のモデルの癖ではなく")
    w("**段階の記述だけで実例（アンカー）を持たないルーブリックの性質**と読むべきである。")
    w("")

    w("### 3.2 観点別（平均 / σ / 使った段階の数）")
    w("")
    w("| 観点 | 満点 | " + " | ".join(names) + " |")
    w("|---" * (len(names) + 2) + "|")
    for code in CODES:
        cells = []
        for name in names:
            values = [lv[code] for lv in raters[name].values() if code in lv]
            cells.append(
                f"{statistics.fmean(values):.2f} / {statistics.pstdev(values):.2f}"
                f" / {len(set(values))}"
            )
        w(f"| {TITLES[code].split('（')[0]} | {rubric.POINTS[code]} | " + " | ".join(cells) + " |")
    w("")

    # ---- 4. 点差がどの観点から来るか --------------------------------------
    w("## 4. 各採点者の「差の付け方」はどの観点から来ているか")
    w("")
    w("観点 c の合計点への寄与を `cov(level_c, total) / var(total)` で見る")
    w("（全観点の和は 1.0）。**その採点者が誰を上位に置くかを決めているのは")
    w("どの観点か**を表す。")
    w("")
    w("| 観点 | 満点 | " + " | ".join(names) + " |")
    w("|---" * (len(names) + 2) + "|")
    contributions: dict[str, dict[str, float]] = {}
    for code in CODES:
        row = []
        for name in names:
            pairs = [
                (raters[name][login][code], tot[name][login])
                for login in tot[name]
                if code in raters[name][login]
            ]
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            var = statistics.pvariance(ys)
            if var == 0:
                row.append("—")
                continue
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / len(xs)
            share = cov / var
            contributions.setdefault(name, {})[code] = share
            row.append(f"{share:.2f}")
        w(f"| {TITLES[code].split('（')[0]} | {rubric.POINTS[code]} | " + " | ".join(row) + " |")
    w("")

    # ---- 5. 対ごとの一致（観点別） ---------------------------------------
    w("## 5. 観点別の一致度")
    w("")
    for code in CODES:
        w(f"### {TITLES[code]}")
        w("")
        w("| 組 | n | 完全一致 | κ | QWK |")
        w("|---|--:|--:|--:|--:|")
        for x, y in itertools.combinations(names, 2):
            shared = sorted(set(raters[x]) & set(raters[y]))
            pairs = [
                (raters[x][i][code], raters[y][i][code])
                for i in shared
                if code in raters[x][i] and code in raters[y][i]
            ]
            if len(pairs) < 2:
                continue
            a = [p[0] for p in pairs]
            b = [p[1] for p in pairs]
            w(
                f"| {x} ↔ {y} | {len(a)} | {exact_agreement(a, b):.1%}"
                f" | {fmt(cohen_kappa(a, b))}"
                f" | {fmt(quadratic_weighted_kappa(a, b, range(rubric.POINTS[code] + 1)))} |"
            )
        units = [
            [raters[n][login].get(code) if login in raters[n] else None for n in names]
            for login in logins
        ]
        alpha = krippendorff_alpha_ordinal(units, list(range(rubric.POINTS[code] + 1)))
        w("")
        w(f"5 者まとめて: **Krippendorff の α = {fmt(alpha)}**")
        w("")

    # ---- 6. 合計点 -------------------------------------------------------
    w("## 6. 合計点の一致度")
    w("")
    w("**r（ピアソン）と QWK を並べて見る。** r は順位だけを見るので、一律の")
    w("ずれや幅の違いでは下がらない。QWK は同じ値を付けたかを見るので下がる。")
    w("両者がずれていれば「同じ順に、ずれた位置で並べている」と読める。")
    w("")
    w("| 組 | n | r | ρ（順位） | τ | QWK | 平均差 | 平均絶対差 |")
    w("|---|--:|--:|--:|--:|--:|--:|--:|")
    for x, y in itertools.combinations(names, 2):
        shared = sorted(set(tot[x]) & set(tot[y]))
        if len(shared) < 3:
            continue
        a = [tot[x][i] for i in shared]
        b = [tot[y][i] for i in shared]
        diffs = [q - p for p, q in zip(a, b, strict=True)]
        w(
            f"| {x} ↔ {y} | {len(a)} | {fmt(pearson(a, b))} | {fmt(spearman(a, b))}"
            f" | {fmt(kendall_tau(a, b))}"
            f" | {fmt(quadratic_weighted_kappa(a, b, range(rubric.TOTAL_POINTS + 1)))}"
            f" | {statistics.fmean(diffs):+.2f}"
            f" | {statistics.fmean(abs(d) for d in diffs):.2f} |"
        )
    units = [
        [tot[n].get(login) for n in names]
        for login in logins
    ]
    alpha = krippendorff_alpha_ordinal(units, list(range(rubric.TOTAL_POINTS + 1)))
    w("")
    w(f"5 者まとめて: **Krippendorff の α = {fmt(alpha)}**")
    w("")

    # ---- 7. 分解 ---------------------------------------------------------
    w("## 7. 不一致を分解する — 偏り・尺度・順位")
    w("")
    w("不一致は 3 つに分かれる。**偏り**（平均のずれ）と**尺度**（幅の比）は")
    w("1 次式で直せる。**順位**（誰を上に置くか）は直せない。")
    w("")
    w("| 組 | 平均差 | 尺度比 σy/σx | 順位の一致 ρ | QWK |")
    w("|---|--:|--:|--:|--:|")
    for x, y in itertools.combinations(names, 2):
        shared = sorted(set(tot[x]) & set(tot[y]))
        if len(shared) < 3:
            continue
        a = [tot[x][i] for i in shared]
        b = [tot[y][i] for i in shared]
        sx, sy = statistics.pstdev(a), statistics.pstdev(b)
        ratio = f"{sy / sx:.2f}" if sx else "—"
        qwk = quadratic_weighted_kappa(a, b, range(rubric.TOTAL_POINTS + 1))
        w(
            f"| {x} ↔ {y} | {statistics.fmean(b) - statistics.fmean(a):+.2f}"
            f" | {ratio} | {fmt(spearman(a, b))} | {fmt(qwk)} |"
        )
    w("")

    # ---- 8. 較正の検定 ---------------------------------------------------
    w("## 8. 較正で直るのか（1 件抜き交差検証）")
    w("")
    w("「順位は合っているが尺度が合っていない」なら、**1 次式で写せば一致度は")
    w("上がるはず**である。これを検定する。")
    w("")
    w("**同じ 19 件で当てはめて測ると必ず上がる**ので、各件の変換は")
    w("その件を除いた 18 件から決める（1 件抜き交差検証）。上がらなければ、")
    w("問題は較正ではなく**何を見ているかの違い**である。")
    w("")
    w("| モデル → CSV | QWK（そのまま） | QWK（較正後） | 変化 | 平均絶対差 前→後 |")
    w("|---|--:|--:|--:|--:|")
    base_name = names[0]
    for name in names[1:]:
        shared = sorted(set(tot[base_name]) & set(tot[name]))
        if len(shared) < 4:
            continue
        source = [tot[name][i] for i in shared]
        target = [tot[base_name][i] for i in shared]
        before = quadratic_weighted_kappa(
            target, source, range(rubric.TOTAL_POINTS + 1)
        )
        mapped = recalibrated_loo(source, target, rubric.TOTAL_POINTS)
        after = quadratic_weighted_kappa(
            target, mapped, range(rubric.TOTAL_POINTS + 1)
        )
        mae_before = statistics.fmean(
            abs(s - t) for s, t in zip(source, target, strict=True)
        )
        mae_after = statistics.fmean(
            abs(m - t) for m, t in zip(mapped, target, strict=True)
        )
        w(
            f"| {name} | {fmt(before)} | {fmt(after)} | {after - before:+.3f}"
            f" | {mae_before:.2f} → {mae_after:.2f} |"
        )
    w("")

    # ---- 9. 10 点上限の判定 ----------------------------------------------
    w("## 9. 成績を左右する 1 つの決定 — 10 点上限")
    w("")
    w("CSV の採点では、実験先が 0（localhost だけ）のレポートは")
    w("**20 点満点中 10 点が上限**になる。他のどの観点よりも成績への効き方が大きい")
    w("単一の判定なので、ここだけを 2 値で取り出す。")
    w("")
    w(
        "| 採点者 | 上限と判定 | CSV と一致"
        " | 見逃し（CSV は上限・モデルは上限でない） | 過剰（逆） |"
    )
    w("|---|--:|--:|--:|--:|")
    capped_csv = {i for i in raters[base_name] if raters[base_name][i].get("target") == 0}
    for name in names:
        rater = raters[name]
        shared = sorted(set(rater) & set(raters[base_name]))
        capped = {i for i in shared if rater[i].get("target") == 0}
        truth = {i for i in shared if i in capped_csv}
        agree = sum(1 for i in shared if (i in capped) == (i in truth))
        missed = sorted(truth - capped)
        extra = sorted(capped - truth)
        w(
            f"| {name} | {len(capped)} | {agree}/{len(shared)}"
            f" | {len(missed)}{' ' + '・'.join(missed) if missed else ''}"
            f" | {len(extra)}{' ' + '・'.join(extra) if extra else ''} |"
        )
    w("")

    # ---- 10. アンサンブル -------------------------------------------------
    w("## 10. モデルの中央値は単独より良いか")
    w("")
    w("観点ごとに 4 モデルの**中央値**を取った仮想の採点者を作り、CSV と比べる。")
    w("")
    model_names = names[1:]
    ensemble: dict[str, dict[str, int]] = {}
    for login in logins:
        levels = {}
        for code in CODES:
            values = [
                raters[n][login][code]
                for n in model_names
                if login in raters[n] and code in raters[n][login]
            ]
            if values:
                levels[code] = int(statistics.median_low(values))
        if levels:
            ensemble[login] = levels
    ens_tot = totals(ensemble)
    shared = sorted(set(tot[base_name]) & set(ens_tot))
    a = [tot[base_name][i] for i in shared]
    b = [ens_tot[i] for i in shared]
    w("| 採点者 | n | r | QWK | 平均差 | 平均絶対差 |")
    w("|---|--:|--:|--:|--:|--:|")
    for name in model_names:
        s2 = sorted(set(tot[base_name]) & set(tot[name]))
        p = [tot[base_name][i] for i in s2]
        q = [tot[name][i] for i in s2]
        d = [y - x for x, y in zip(p, q, strict=True)]
        w(
            f"| {name} | {len(p)} | {fmt(pearson(p, q))}"
            f" | {fmt(quadratic_weighted_kappa(p, q, range(rubric.TOTAL_POINTS + 1)))}"
            f" | {statistics.fmean(d):+.2f} | {statistics.fmean(abs(v) for v in d):.2f} |"
        )
    d = [y - x for x, y in zip(a, b, strict=True)]
    w(
        f"| **4 モデルの中央値** | {len(a)} | {fmt(pearson(a, b))}"
        f" | {fmt(quadratic_weighted_kappa(a, b, range(rubric.TOTAL_POINTS + 1)))}"
        f" | {statistics.fmean(d):+.2f} | {statistics.fmean(abs(v) for v in d):.2f} |"
    )
    w("")

    # ---- 11. 提出ごとの割れ方 --------------------------------------------
    w("## 11. どのレポートで割れるか")
    w("")
    w("提出ごとに、5 者の合計点の幅（最大 − 最小）を見る。")
    w("")
    w("| 学生 | " + " | ".join(names) + " | 幅 | CSV の位置 |")
    w("|---" * (len(names) + 3) + "|")
    rows = []
    for login in logins:
        values = {n: tot[n].get(login) for n in names}
        present = [v for v in values.values() if v is not None]
        if len(present) < 2:
            continue
        rows.append((max(present) - min(present), login, values, present))
    for span, login, values, present in sorted(rows, reverse=True):
        csv_value = values[base_name]
        if csv_value is None:
            position = "—"
        elif csv_value == max(present):
            position = "最も甘い"
        elif csv_value == min(present):
            position = "最も辛い"
        else:
            position = "中間"
        cells = " | ".join(
            str(values[n]) if values[n] is not None else "—" for n in names
        )
        w(f"| {login} | {cells} | {span} | {position} |")
    w("")

    text = "\n".join(out) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(out)} 行 → {args.out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
