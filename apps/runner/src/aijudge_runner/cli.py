"""`aijudge-runner` — ブラウザ IDE の試しの実行を処理する（ADR 0024）。

    uv run aijudge-runner                  # 常駐して待つ
    uv run aijudge-runner --once           # 待っている要求が無くなったら終わる

**採点ワーカーとは別のプロセスである。** 採点キューには触らず、runner が
止まっても採点は進む。同時に動かす数は、このプロセスを何本立てるかで決める
（`aijudge-runner@1..K`、運用機では K = 8 から）── プロセスの中に並列性を
持たせない。

**起動時に sandbox を組み立てる。** 組み立てられなければ起動に失敗する ──
実行できない runner が要求を取っては失敗させ続けるより、systemd に知らせる
方がよい。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from aijudge_persistence import ENV_DATABASE_URL, Database
from aijudge_sandbox import SandboxError
from aijudge_telemetry import configure_logging

from .runner import CodeRunner

REPO_ROOT = Path(__file__).resolve().parents[4]

# 科目プロファイルの置き場所。**採点ワーカーと同じ場所を指すこと** ──
# runner だけ古い宣言を読むと、IDE で動いたものが採点で動かない。
ENV_PROFILES_DIR = "AIJUDGE_PROFILES_DIR"
# 待っている要求を見に行く間隔（秒、設計書 §8.2）。学習者は画面の前で
# 待っているので、採点ワーカー（2 秒）よりずっと短い。
DEFAULT_POLL_SECONDS = 0.25

_stopping = False


def _stop(*_: object) -> None:
    global _stopping
    _stopping = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-runner", description="ブラウザ IDE の試しの実行を処理する（採点ではない）"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(ENV_DATABASE_URL),
        help=f"接続先（既定: ${ENV_DATABASE_URL}）",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path(os.environ.get(ENV_PROFILES_DIR, REPO_ROOT / "subjects")).expanduser(),
        help="科目プロファイルの場所（採点ワーカーと同じ場所を指すこと）",
    )
    parser.add_argument("--name", default=os.uname().nodename, help="runner の識別子")
    parser.add_argument("--once", action="store_true", help="待っている要求が無くなったら終わる")
    parser.add_argument(
        "--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="常駐時の待ち間隔"
    )
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="スキーマが無ければ作る（開発用。運用中のデータがある環境では使わない）",
    )
    args = parser.parse_args(argv)

    configure_logging(f"runner-{args.name}")
    database = Database.connect(args.database_url, create=args.create_schema)
    runner = CodeRunner(database, profiles_dir=args.profiles, worker=args.name)

    if not database.supports_row_locking:
        # 行ロックが無い環境で複数立てると、同じ要求を二度動かす。
        print(
            "警告: この接続先は行ロックを持ちません（SQLite）。"
            "runner は 1 プロセスだけにしてください。",
            file=sys.stderr,
        )

    try:
        try:
            sandbox = runner.warm_up()
        except SandboxError as exc:
            print(f"sandbox を用意できません: {exc}", file=sys.stderr)
            return 1

        if args.once:
            count = runner.run_until_empty()
            print(f"処理した要求: {count} 件")
            return 0

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        print(f"runner {args.name} を開始しました（sandbox: {sandbox.name}、Ctrl-C で停止）")
        while not _stopping:
            if runner.run_once() is None:
                time.sleep(args.poll_seconds)
        print("停止しました")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
