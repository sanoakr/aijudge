"""`aijudge-worker` — 採点ワーカーを走らせる。

    uv run aijudge-worker --once            # キューが空になるまで処理して終わる
    uv run aijudge-worker                   # 常駐して待つ
    uv run aijudge-worker --subject cs_intro_c   # 科目を絞る（GPU の割り当てを分ける）
    uv run aijudge-worker --phase deterministic  # 速い段階だけを担当する

**レビューとは独立に走る。** レビューは採点の前提条件ではない（ADR 0007）。

**段階を分けて立てる。** 決定的評価は 1 秒未満、AI 評価は十数秒（実測 12.8 秒、
うち 95% が LLM）。同じワーカーに任せると、テスト実行が終わっている提出の結果が
前に並んだ他人の LLM 待ちの後ろで止まる。`--phase deterministic` を 1 本立てれば、
決定的評価の結果は待たされない（設計方針 §9.1 の「p95 < 30 秒」はこの形で満たす）。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from aijudge_core import GradingPhase
from aijudge_persistence import ENV_DATABASE_URL, Database, ObservationFileStore
from aijudge_submission import FilesystemArtifactStore

from .feedback import build_feedback_generator
from .worker import GradingWorker

REPO_ROOT = Path(__file__).resolve().parents[4]

ENV_ARTIFACT_DIR = "AIJUDGE_ARTIFACT_DIR"
ENV_OBSERVATION_DIR = "AIJUDGE_OBSERVATION_DIR"
DEFAULT_ARTIFACT_DIR = Path.home() / ".aijudge" / "artifacts"
DEFAULT_OBSERVATION_DIR = Path.home() / ".aijudge" / "observations"

_stopping = False


def _stop(*_: object) -> None:
    global _stopping
    _stopping = True


def build_worker(args: argparse.Namespace) -> tuple[GradingWorker, Database]:
    database = Database.connect(args.database_url, create=args.create_schema)
    worker = GradingWorker(
        database,
        FilesystemArtifactStore(args.artifacts),
        profiles_dir=args.profiles,
        observations=ObservationFileStore(args.observations),
        feedback=build_feedback_generator(),
        worker=args.name,
    )
    return worker, database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-worker", description="採点ジョブを処理する（レビューとは独立）"
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
        help="提出物の置き場所",
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path(os.environ.get(ENV_OBSERVATION_DIR, DEFAULT_OBSERVATION_DIR)).expanduser(),
        help="観測レコードの置き場所（測定用。無くても採点は動く）",
    )
    parser.add_argument(
        "--profiles", type=Path, default=REPO_ROOT / "subjects", help="科目プロファイルの場所"
    )
    parser.add_argument("--subject", default=None, help="この科目のジョブだけ処理する")
    parser.add_argument(
        "--phase",
        default=None,
        choices=[phase.value for phase in GradingPhase],
        help=(
            "この段階のジョブだけ処理する。deterministic 専用のワーカーを 1 本立てると、"
            "テスト実行の結果が AI 待ちの後ろで止まらない"
        ),
    )
    parser.add_argument("--name", default=os.uname().nodename, help="ワーカーの識別子")
    parser.add_argument("--once", action="store_true", help="キューが空になったら終わる")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="常駐時の待ち間隔")
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="スキーマが無ければ作る（開発用。運用中のデータがある環境では使わない）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    phase = None if args.phase is None else GradingPhase(args.phase)
    worker, database = build_worker(args)

    if not database.supports_row_locking:
        # 行ロックが無い環境で複数ワーカーを立てると同じ提出を二度採点する。
        print(
            "警告: この接続先は行ロックを持ちません（SQLite）。"
            "ワーカーは 1 プロセスだけにしてください。",
            file=sys.stderr,
        )

    try:
        if args.once:
            graded, errors = worker.run_until_empty(subject_profile=args.subject, phase=phase)
            print(f"採点した提出: {graded} 件")
            for error in errors:
                print(f"  失敗: {error}", file=sys.stderr)
            return 1 if errors else 0

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        print(f"ワーカー {args.name} を開始しました（Ctrl-C で停止）")
        while not _stopping:
            result = worker.run_once(subject_profile=args.subject, phase=phase)
            if result is None:
                time.sleep(args.poll_seconds)
                continue
            if result.graded:
                print(f"採点しました: {result.job.submission_id}")
            else:
                print(f"失敗: {result.job.submission_id}: {result.error}", file=sys.stderr)
        print("停止しました")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
