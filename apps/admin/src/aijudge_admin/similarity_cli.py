"""`aijudge-similarity` — 受付が閉じた課題の、提出どうしの類似を調べる（#203・ADR 0029）。

    uv run aijudge-similarity --once               # 1 回走って終わる（timer が呼ぶ）
    uv run aijudge-similarity --once --dry-run     # どの課題が対象かだけ見る
    uv run aijudge-similarity --once --course crs_… # 1 コースに絞る

**締切の後は提出の処理が無く、運用機が空いている**（#203 の決定 1）。受付終了（無ければ
締切）の 1 時間後から、**採点が終わり**入力が変わった課題だけを回す。消えた提出の報告は
同じ周回で消す。

**確定には相乗りさせない。** 確定は成績を閉じる処理で、検査はそれを読んではいけない
（`.importlinter` の `grades-do-not-read-similarity`）。混ぜると、検査を止めたいときに
確定も止まる。

**置き場所が無ければ何もせずに 0 で終わる。** 運用機で 1 度だけ置く設定
（`AIJUDGE_SIMILARITY_DIR`・docs/RUNNING.md）の前に timer が動いても、失敗の知らせを
毎時送らない。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from aijudge_core.ids import CourseId
from aijudge_course_admin import code_similarity
from aijudge_persistence import ENV_DATABASE_URL, Database
from aijudge_submission import FilesystemArtifactStore
from aijudge_telemetry import configure_logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aijudge-similarity",
        description="受付が閉じた課題の、提出どうしの類似を Dolos で調べる（担当教員だけが見る）",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(ENV_DATABASE_URL),
        help=f"接続先（既定: ${ENV_DATABASE_URL}）",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(
            os.environ.get("AIJUDGE_ARTIFACT_DIR", Path.home() / ".aijudge" / "artifacts")
        ).expanduser(),
        help="提出物の置き場所（web の AIJUDGE_ARTIFACT_DIR と同じ場所）",
    )
    parser.add_argument(
        "--similarity-dir",
        type=Path,
        default=code_similarity.similarity_root(),
        help=f"報告の置き場所（既定: ${code_similarity.ENV_SIMILARITY_DIR}）",
    )
    parser.add_argument("--course", default=None, help="このコースだけを対象にする")
    parser.add_argument("--once", action="store_true", help="1 回走って終わる（timer 向き）")
    parser.add_argument("--dry-run", action="store_true", help="対象の課題を表示するだけ")
    parser.add_argument(
        "--now", default=None, help="この時刻を「今」として判定する（ISO 8601、検証用）"
    )
    args = parser.parse_args(argv)

    configure_logging("similarity")

    if not args.once:
        # 常駐させる理由が無い（締切は時間単位で来る）。timer から 1 回ずつ呼ぶ。
        print("--once を付けてください（timer から 1 回ずつ呼びます）", file=sys.stderr)
        return 2
    if args.similarity_dir is None:
        logger.info(
            "%s が無いので何もしません（docs/RUNNING.md「提出どうしの類似」）",
            code_similarity.ENV_SIMILARITY_DIR,
        )
        return 0

    now: datetime | None = None
    if args.now is not None:
        try:
            parsed = datetime.fromisoformat(args.now)
        except ValueError:
            print(f"--now の形式が不正です: {args.now!r}", file=sys.stderr)
            return 2
        now = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    database = Database.connect(args.database_url)
    try:
        report = code_similarity.sweep(
            database,
            root=args.similarity_dir,
            artifact_store=FilesystemArtifactStore(args.artifacts),
            now=now,
            course_id=None if args.course is None else CourseId(args.course),
            dry_run=args.dry_run,
        )
    finally:
        database.dispose()

    if args.dry_run:
        for task in report.due:
            logger.info("対象: %s [%s] %s", task.unit_label, task.title, task.id)
        logger.info("受付が閉じた課題: %d 件（入力が前回と同じものは回しません）", len(report.due))
        return 0
    for path in report.removed:
        logger.info("元の提出が消えたので報告を消しました: %s", path)
    for run in report.ran:
        logger.info("%s", code_similarity.run_payload(run))
    for waiting in report.grading:
        logger.info("採点を待っています（次の周回で回します）: %s", waiting)
    logger.info(
        "回した課題 %d 件・採点待ち %d 件・失敗 %d 件・消した報告 %d 件",
        len(report.ran),
        len(report.grading),
        len(report.failed),
        len(report.removed),
    )
    # **1 課題の失敗は次の周回で回し直す。** ここで非 0 を返すと、毎時の知らせが
    # 同じ失敗で埋まる。失敗はログと run.json（NOT_MEASURED）に残る。
    return 0


if __name__ == "__main__":
    sys.exit(main())
