"""`aijudge-eval` — 採点精度の測定コマンド。

    uv run aijudge-eval --subject cs_intro_c

終了コードは 3 種類にしてある。CI が「測れていない」を「合格」と
取り違えないようにするため。

    0  合格
    1  不合格（基準を下回った）
    2  測定できない（教員採点データが足りない）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from aijudge_analytics import Gates, Verdict

from .golden import ENV_GOLDEN_DIR, GoldenSetError, golden_root, load_golden
from .runner import EvalReport, run_evaluation

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NOT_MEASURED = 2

REPO_ROOT = Path(__file__).resolve().parents[4]


def load_gates(path: Path) -> Gates:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a mapping")
    return Gates.model_validate(data)


def render(report: EvalReport) -> str:
    """人が読む報告。数字だけでなく「なぜ測れないか」も出す。"""
    lines: list[str] = []
    lines.append(f"# {report.poc} 採点精度レポート — {report.subject_profile}")
    lines.append("")
    lines.append(f"生成: {report.generated_at:%Y-%m-%d %H:%M:%S %Z}")
    lines.append(f"採点した提出: {report.item_count} 件")
    if report.regraded_runs:
        lines.append("")
        lines.append(
            f"> このうち {report.regraded_runs} 件は測定のために採点し直しています"
            f"（教員がレビューした採点をそのまま使ったのは {report.reused_runs} 件）。"
            "引き直した分の見逃し率は実績ではなく推定です。"
        )
    lines.append("")

    if report.agreement:
        lines.append("## 観点別の一致度")
        lines.append("")
        lines.append("| 観点 | 標本 | 完全一致 | Cohen's κ | QWK | 偏り |")
        lines.append("|------|-----:|--------:|----------:|----:|-----:|")
        for code, item in sorted(report.agreement.items()):

            def fmt(value: float | None) -> str:
                return "—" if value is None else f"{value:.3f}"

            lines.append(
                f"| {code} | {item.sample_size} | {fmt(item.exact_agreement)} "
                f"| {fmt(item.cohen_kappa)} | {fmt(item.quadratic_weighted_kappa)} "
                f"| {fmt(item.mean_bias)} |"
            )
        lines.append("")

        for code, item in sorted(report.agreement.items()):
            if not item.confusion:
                continue
            lines.append(f"### {code} の混同行列（行=教員 / 列=AI）")
            lines.append("")
            header = " | ".join(f"AI {level}" for level in item.levels)
            lines.append(f"| | {header} |")
            lines.append("|---|" + "---:|" * len(item.levels))
            for level, row in zip(item.levels, item.confusion, strict=True):
                cells = " | ".join(str(count) for count in row)
                lines.append(f"| 教員 {level} | {cells} |")
            lines.append("")

    lines.append("## 運用の指標")
    lines.append("")
    for label, value in (
        ("見逃し率（自動確定したが教員が修正）", report.observed_miss_rate),
        ("レビュー行き率", report.observed_review_rate),
        ("採点のばらつき（反復時の標準偏差）", report.observed_score_stdev),
    ):
        shown = "測定なし" if value is None else f"{value:.3f}"
        lines.append(f"- {label}: {shown}")
    lines.append("")

    lines.append("## 合格基準")
    lines.append("")
    lines.append("| 判定 | 基準 | 実測 | 閾値 | 備考 |")
    lines.append("|------|------|-----:|-----:|------|")
    for check in report.checks:
        observed = "—" if check.observed is None else f"{check.observed:.3f}"
        threshold = "—" if check.threshold is None else f"{check.threshold:.3f}"
        lines.append(
            f"| {check.symbol} | {check.name} | {observed} | {threshold} | {check.detail} |"
        )
    lines.append("")

    verdict_text = {
        Verdict.PASS: "合格",
        Verdict.FAIL: "不合格",
        Verdict.NOT_MEASURED: "判定不能（データ不足）",
    }[report.verdict]
    lines.append(f"**総合判定: {verdict_text}**")

    if report.verdict is Verdict.NOT_MEASURED:
        lines.append("")
        lines.append(
            "測れていないことは合格ではない。教員採点データを "
            f"`${ENV_GOLDEN_DIR}` 配下に用意してから再実行すること。"
        )

    if report.errors:
        lines.append("")
        lines.append("## 失敗した項目")
        lines.append("")
        for error in report.errors:
            lines.append(f"- {error}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-eval",
        description="教員採点と AI 採点の一致度を測り、PoC の合格基準と突き合わせる",
    )
    parser.add_argument("--subject", default="cs_intro_c", help="科目プロファイル名")
    parser.add_argument(
        "--golden",
        type=Path,
        default=None,
        help=f"ゴールデンセットの場所（既定: ${ENV_GOLDEN_DIR} または ~/.aijudge/golden）",
    )
    parser.add_argument(
        "--gates", type=Path, default=REPO_ROOT / "evals" / "gates.yaml", help="合格基準の定義"
    )
    parser.add_argument(
        "--profiles", type=Path, default=REPO_ROOT / "subjects", help="科目プロファイルの場所"
    )
    parser.add_argument("--out", type=Path, default=None, help="Markdown レポートの出力先")
    parser.add_argument("--json", type=Path, default=None, help="JSON の出力先")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="採点の一貫性を測るために先頭 1 件を繰り返す回数",
    )
    parser.add_argument(
        "--regrade",
        action="store_true",
        help="保存済みの採点を使わず引き直す（教員が見たのとは別の採点を測ることになる）",
    )
    parser.add_argument(
        "--include-non-blind",
        action="store_true",
        help="AI の結果を見てから付けた採点も含める（既定では除外する）",
    )
    args = parser.parse_args(argv)

    root = args.golden or golden_root()
    try:
        items = load_golden(root, args.subject, blind_only=not args.include_non_blind)
    except GoldenSetError as exc:
        print(f"ゴールデンセットが壊れています: {exc}", file=sys.stderr)
        return EXIT_NOT_MEASURED

    gates = load_gates(args.gates)
    profile_path = args.profiles / f"{args.subject}.yaml"

    if not items:
        print(
            f"{root} に {args.subject} の教員採点データがありません。\n"
            f"AI 採点の精度は測定できません（合格ではありません）。\n"
            f"形式は evals/golden/README.md を参照してください。",
            file=sys.stderr,
        )
        return EXIT_NOT_MEASURED

    report = run_evaluation(
        items,
        gates=gates,
        subject_profile=args.subject,
        profile_path=profile_path,
        repeats=args.repeats,
        regrade=args.regrade,
        progress=lambda line: print(line, file=sys.stderr),
    )

    text = render(report)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.json:
        args.json.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        Verdict.PASS: EXIT_PASS,
        Verdict.FAIL: EXIT_FAIL,
        Verdict.NOT_MEASURED: EXIT_NOT_MEASURED,
    }[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
