"""`aijudge-ide-close` — 受付を終えたエディタの課題で、自動保存の最新を提出する。

    uv run aijudge-ide-close --once              # 1 回走って終わる（timer 向き）
    uv run aijudge-ide-close --once --dry-run    # 何が出るかだけ見る

設計書 §9.1。**何度走らせても同じ結果になる**（提出は内容で重複を畳む）ので、
systemd の timer で 1 分ごとに `--once` を流す。受付終了から最大 1 分で出る。

**採点ワーカーにも runner にも相乗りさせない。** runner は K 本立てるので、
同じ掃除を K 回走らせることになる。採点ワーカーは提出を作る側ではない。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from aijudge_persistence import ENV_DATABASE_URL, Database
from aijudge_submission import FilesystemArtifactStore
from aijudge_telemetry import configure_logging

from .autosubmit import DEFAULT_LOOKBACK_HOURS, close_editor_tasks
from .cli import RUNNER_MAX_OVERFLOW, RUNNER_POOL_SIZE

logger = logging.getLogger(__name__)

# 提出物の置き場所。**web・採点ワーカーと同じ場所を指すこと**（同じ変数名）。
ENV_ARTIFACT_DIR = "AIJUDGE_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR = Path.home() / ".aijudge" / "artifacts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-ide-close",
        description="受付を終えたエディタの課題で、自動保存の最新を提出する",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(ENV_DATABASE_URL),
        help=f"接続先（既定: ${ENV_DATABASE_URL}）",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get(ENV_ARTIFACT_DIR, DEFAULT_ARTIFACT_DIR)).expanduser(),
        help="提出物の置き場所（web・採点ワーカーと同じ場所）",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=DEFAULT_LOOKBACK_HOURS,
        help="受付終了からどれだけ遡るか（冪等なので重なっても害は無い）",
    )
    parser.add_argument("--once", action="store_true", help="1 回走って終わる（既定もこれ）")
    parser.add_argument("--dry-run", action="store_true", help="提出せず、何が出るかだけ見る")
    args = parser.parse_args(argv)

    configure_logging("ide-close")
    # 1 回走って終わる処理なので、接続の枠は小さく取る（runner と同じ）。
    database = Database.connect(
        args.database_url, pool_size=RUNNER_POOL_SIZE, max_overflow=RUNNER_MAX_OVERFLOW
    )
    try:
        report = close_editor_tasks(
            database,
            FilesystemArtifactStore(args.artifacts),
            now=datetime.now(UTC),
            lookback_hours=args.lookback_hours,
            dry_run=args.dry_run,
        )
    finally:
        database.dispose()

    verb = "提出する予定" if args.dry_run else "提出しました"
    # **出さなかった件数と理由も必ず出す**（finalize と同じ作法）。黙って積み
    # 上がると、自動提出が効いていないことに試験の後で気づく。
    logger.info(
        "%s: %d 件 / 同じ内容を提出済み: %d 件 / 見送り: %s",
        verb,
        report.submitted,
        report.unchanged,
        ", ".join(f"{k} {v} 件" for k, v in sorted(report.skipped.items())) or "なし",
    )
    for failure in report.failures:
        print(f"失敗: {failure}", file=sys.stderr)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
