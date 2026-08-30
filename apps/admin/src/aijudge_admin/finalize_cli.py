"""`aijudge-finalize` — 締切を過ぎた課題の成績を自動確定する。

    uv run aijudge-finalize --once                # 1 回走って終わる（cron 向き）
    uv run aijudge-finalize                       # 常駐して定期的に走る
    uv run aijudge-finalize --once --dry-run      # 何が確定するかだけ見る
    uv run aijudge-finalize --once --course crs_… # 1 コースに絞る

猶予は**問題セットごと**の設定（無ければコースの既定・`auto_finalize_after_minutes`）。教員が
`/manage` で入れる。入っていないコースは何もしない ── 既定は「自動確定
しない」であり、設定漏れではなく設定内容である。

**採点ワーカーには相乗りさせない。** 確定はレビュー側の判断で、採点側が
それを知る必要はない（`.importlinter` の「採点とレビュー」の分離）。混ぜると、
採点を止めたいときに確定も止まる。

**何度走らせても同じ結果になる。** 確定済みの採点は候補に上がらず、
上がっても保存が拒否される（1 採点につき確定は 1 つ）。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime

from aijudge_core.ids import CourseId
from aijudge_persistence import ENV_DATABASE_URL, Database

from .finalization import FinalizeReport, sweep_deadlines

# 常駐時の既定の間隔。締切の猶予は時間単位なので、分単位で回す意味がない。
DEFAULT_INTERVAL_SECONDS = 900.0

_stopping = False


def _stop(*_: object) -> None:
    global _stopping
    _stopping = True


def _report(report: FinalizeReport, *, dry_run: bool) -> None:
    """**見送った件数と理由を必ず出す。**

    「確定 12 件」だけでは、残った 3 件が異議申立なのか採点の失敗なのか
    運用者に分からない。自動確定は毎日走るので、そこで黙って積み上がる
    ものが見えないと学期末に気づくことになる。
    """
    prefix = "確定する予定" if dry_run else "確定しました"
    if not report.touched:
        logging.info("対象はありませんでした")
        return
    for outcome in report.touched:
        parts = [f"{prefix}: {outcome.finalized} 件"]
        if outcome.contested:
            parts.append(f"異議申立あり（教員が確認）: {outcome.contested} 件")
        if outcome.needs_review:
            parts.append(f"要レビュー（自動確定しない）: {outcome.needs_review} 件")
        if outcome.provisional:
            parts.append(f"未採点の観点あり: {outcome.provisional} 件")
        logging.info("%s [%s] %s", outcome.task.unit_label, outcome.task.title, " / ".join(parts))
    logging.info("合計 %d 件確定、%d 件見送り", report.finalized, report.skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-finalize",
        description="締切から所定の時間が過ぎた課題の成績を自動確定する",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(ENV_DATABASE_URL),
        help=f"接続先（既定: ${ENV_DATABASE_URL}）",
    )
    parser.add_argument("--course", default=None, help="このコースだけを対象にする")
    parser.add_argument("--once", action="store_true", help="1 回走って終わる（cron 向き）")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"常駐時の実行間隔（既定: {DEFAULT_INTERVAL_SECONDS:.0f} 秒）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="何が確定するかだけ表示して、保存しない",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="この時刻を「今」として判定する（ISO 8601、検証用）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    now: datetime | None = None
    if args.now is not None:
        try:
            parsed = datetime.fromisoformat(args.now)
        except ValueError:
            print(f"--now の形式が不正です: {args.now!r}", file=sys.stderr)
            return 2
        # 素の値を UTC として扱う。ローカル時刻で判定すると、確定の時刻が
        # サーバの設定に依存する（RUNNING.md の締切と同じ規則）。
        now = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    course_id = None if args.course is None else CourseId(args.course)
    database = Database.connect(args.database_url)
    try:
        if args.once:
            _report(
                sweep_deadlines(database, now=now, course_id=course_id, dry_run=args.dry_run),
                dry_run=args.dry_run,
            )
            return 0

        if args.now is not None:
            # 常駐で固定時刻を使うと、同じ判定を延々と繰り返すだけになる。
            print("--now は --once と併せて使ってください", file=sys.stderr)
            return 2

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        logging.info("自動確定を開始しました（%.0f 秒ごと、Ctrl-C で停止）", args.interval_seconds)
        while not _stopping:
            _report(
                sweep_deadlines(database, course_id=course_id, dry_run=args.dry_run),
                dry_run=args.dry_run,
            )
            # 停止要求に長く待たせない。間隔は時間単位の話なので粗くて構わない。
            waited = 0.0
            while waited < args.interval_seconds and not _stopping:
                time.sleep(min(1.0, args.interval_seconds - waited))
                waited += 1.0
        logging.info("停止しました")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
