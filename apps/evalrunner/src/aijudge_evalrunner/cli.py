"""`aijudge-eval` — 採点精度の測定コマンド。

    uv run aijudge-eval --subject cs_intro_c

**このコマンドは採点しない。** 採点とレビューが残した観測レコードを読み、
指標を計算するだけ（ADR 0007）。採点運用はこのコマンドに依存しない。

終了コードは 3 種類にしてある。CI が「測れていない」を「合格」と
取り違えないようにするため。

    0  合格
    1  不合格（基準を下回った）
    2  測定できない（教員の blind 採点が足りない）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from aijudge_analytics import Gates, Verdict

from .observations import (
    ENV_GOLDEN_DIR,
    ObservationSetError,
    golden_root,
    load_observations,
)
from .runner import EvalReport, measure

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
    lines.append(
        f"観測: {report.observation_count} 件"
        f"（提出 {report.submission_count} 件 / うち blind 採点あり "
        f"{report.blind_submission_count} 件）"
    )
    lines.append("")
    lines.append("> 記録済みの観測を読んだ結果です。このコマンドは採点しません。")
    lines.append("")

    if report.agreement:
        lines.append("## 観点別の一致度")
        lines.append("")
        lines.append("| 観点 | 標本 | 完全一致 | Cohen's κ | QWK | 偏り |")
        lines.append("|------|-----:|--------:|----------:|----:|-----:|")
        for code, item in sorted(report.agreement.items()):
            lines.append(
                f"| {code} | {item.sample_size} | {_fmt(item.exact_agreement)} "
                f"| {_fmt(item.cohen_kappa)} | {_fmt(item.quadratic_weighted_kappa)} "
                f"| {_fmt(item.mean_bias)} |"
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

    if report.excluded:
        lines.append("## 一致度の標本から外した観測")
        lines.append("")
        for reason, count in sorted(report.excluded.items()):
            lines.append(f"- {reason}: {count} 件")
        lines.append("")

    lines.append("## 運用の指標")
    lines.append("")
    for label, value in (
        ("見逃し率（自動確定したが教員が修正）", report.observed_miss_rate),
        ("レビュー行き率", report.observed_review_rate),
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
            "測れていないことは合格ではない。**ただし採点運用は成立している。**"
            "blind 採点は抽出された提出にのみ求めるので、標本が貯まるまで待つか、"
            "科目プロファイルの `measurement.blind_sample_rate` を上げること。"
        )

    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-eval",
        description="記録済みの観測から教員採点と AI 採点の一致度を測る（採点はしない）",
    )
    parser.add_argument("--subject", default="cs_intro_c", help="科目プロファイル名")
    parser.add_argument(
        "--golden",
        type=Path,
        default=None,
        help=f"観測レコードの場所（既定: ${ENV_GOLDEN_DIR} または ~/.aijudge/golden）",
    )
    parser.add_argument(
        "--gates", type=Path, default=REPO_ROOT / "evals" / "gates.yaml", help="合格基準の定義"
    )
    parser.add_argument("--out", type=Path, default=None, help="Markdown レポートの出力先")
    parser.add_argument("--json", type=Path, default=None, help="JSON の出力先")
    args = parser.parse_args(argv)

    root = args.golden or golden_root()
    try:
        observations = load_observations(root, args.subject)
    except ObservationSetError as exc:
        print(f"観測レコードが壊れています: {exc}", file=sys.stderr)
        return EXIT_NOT_MEASURED

    if not observations:
        print(
            f"{root} に {args.subject} の観測レコードがありません。\n"
            f"採点精度は測定できません（合格ではありません）。\n"
            f"採点は `uv run aijudge-grade` で行い、観測はその副産物として書かれます。",
            file=sys.stderr,
        )
        return EXIT_NOT_MEASURED

    report = measure(observations, gates=load_gates(args.gates), subject_profile=args.subject)

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
