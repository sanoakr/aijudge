"""`aijudge-relay` — outbox に溜まったイベントを購読者へ流す。

    uv run aijudge-relay            # 常駐して流し続ける
    uv run aijudge-relay --once     # 空になるまで流して終わる（cron でも可）

## なぜ独立したプロセスなのか

採点結果とイベントは**同じトランザクション**で書かれる（outbox・設計方針
§2.3）ので、「採点が残っているのにイベントが無い」は起こらない。だが書いた
だけでは誰にも届かない ── 未送信を読んで購読者を呼び、送信済みにする人が
要る。それが `EventRelay.drain` で、これはそれを回すだけのプロセスである。

**ワーカーの中に埋めなかった。** 埋めると、動いていないことに気づく手段が
無くなる ── 実際そうなっていた。購読者（`subscribe_skills`）は 2026-08-29 に
書かれ、テストからしか呼ばれないまま運用に出ており、`grading.completed` が
254 件、未送信のまま積み上がっていた（習熟度は 0 件のまま・#328）。

独立したユニットなら、止まっていることが `systemctl --failed` にも設定の
ドリフト検査にも出る。**誰の持ち物でもない配線は、静かに動かなくなる。**

## 1 プロセスだけ立てる

複数立てても壊れない（購読側は冪等で、同じ採点を二度受け取っても習熟度は
一度しか動かない）が、**重複配信を平常にする理由が無い**。再送は落ちた
ときの備えであって、設計の既定ではない。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from aijudge_persistence import ENV_DATABASE_URL, Database
from aijudge_telemetry import configure_logging

from .relay import EventRelay
from .skill_subscriber import subscribe_skills

#: 空のときに待つ秒数。**短くしすぎない** ── 空振りの問い合わせが増えるだけで、
#: イベントは採点の完了に合わせて出るので、数秒の遅れは誰にも見えない。
DEFAULT_POLL_SECONDS = 5.0
#: 1 回の `drain` で読む上限。溜まっているぶんはループが繰り返して片付ける。
BATCH = 100

_stopping = False


def _stop(*_: object) -> None:
    global _stopping
    _stopping = True


def build_relay(database: Database) -> EventRelay:
    """購読者を登録したリレーを組む。

    **登録はここ 1 か所。** 購読者が増えたらここに足す ── 呼び出し側が
    それぞれ登録する形にすると、どのプロセスが何を配っているのかが
    プロセスごとに違うことになる。
    """
    relay = EventRelay(database)
    subscribe_skills(relay, database)
    return relay


def _drain_all(relay: EventRelay) -> int:
    """空になるまで流す。流した件数を返す。

    **1 回の `drain` は上限つき**（`BATCH`）なので、溜まっているぶんは
    繰り返さないと片付かない ── 運用に繋いだ初回がまさにそれで、
    数百件が待っている。
    """
    total = 0
    while True:
        moved = relay.drain(BATCH)
        total += moved
        if moved < BATCH:
            return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-relay",
        description="outbox のイベントを購読者へ流す（習熟度の更新など）",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(ENV_DATABASE_URL),
        help=f"接続先（既定: ${ENV_DATABASE_URL}）",
    )
    parser.add_argument(
        "--once", action="store_true", help="空になるまで流して終わる（cron 運用向け）"
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)

    if not args.database_url:
        print(f"接続先が指定されていません（${ENV_DATABASE_URL}）", file=sys.stderr)
        return 2

    configure_logging("relay")
    database = Database.connect(args.database_url, create=False)
    relay = build_relay(database)
    try:
        if args.once:
            print(f"流したイベント: {_drain_all(relay)} 件")
            return 0

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        print("リレーを開始しました（Ctrl-C で停止）")
        while not _stopping:
            if _drain_all(relay) == 0:
                time.sleep(args.poll_seconds)
        print("停止しました")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
