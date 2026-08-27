"""レビューコンソールと採点ワーカーの起動。

    uv run aijudge-grade  --golden ~/.aijudge/golden          # 採点（提出時に相当）
    uv run aijudge-review --golden ~/.aijudge/golden --marker sano   # 教員レビュー

**採点とレビューは別のコマンドである。** レビューは採点の前提条件ではなく、
採点が届いた提出を教員が確定させる作業（ADR 0007）。

認証は無い。コンソールが既定で 127.0.0.1 にしか bind しないのはそのため。
学内に公開するなら先に S1（Identity）に載せること。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from .app import ENV_GOLDEN_DIR, ENV_MARKER, Console, create_app
from .store import ReviewStore
from .tasks import TaskLoader
from .worker import Grader, grade_pending

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_GOLDEN_DIR = Path.home() / ".aijudge" / "golden"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path(os.environ.get(ENV_GOLDEN_DIR, DEFAULT_GOLDEN_DIR)).expanduser(),
        help="提出物と採点結果の置き場所",
    )
    parser.add_argument(
        "--profiles", type=Path, default=REPO_ROOT / "subjects", help="科目プロファイルの場所"
    )
    parser.add_argument("--subject", default=None, help="科目プロファイル名で絞る")


def grade(argv: list[str] | None = None) -> int:
    """`aijudge-grade` — 採点が無い提出をすべて採点する。

    S3（Submission & Orchestration）の最小代替。本来はキューを消費する
    常駐ワーカーで、提出のたびに走る。
    """
    parser = argparse.ArgumentParser(
        prog="aijudge-grade", description="未採点の提出を採点する（レビューとは独立）"
    )
    _common(parser)
    args = parser.parse_args(argv)

    store = ReviewStore(args.golden)
    grader = Grader(store, args.profiles, TaskLoader())
    graded, errors = grade_pending(
        grader,
        subject_profile=args.subject,
        progress=lambda line: print(line, file=sys.stderr),
    )

    print(f"採点した提出: {graded} 件")
    if errors:
        print(f"失敗: {len(errors)} 件", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        # 一部が失敗しても、採点できた分は成立している。
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """`aijudge-review` — 教員レビューコンソールを起動する。"""
    parser = argparse.ArgumentParser(prog="aijudge-review", description="教員レビューコンソール")
    _common(parser)
    parser.add_argument(
        "--marker",
        default=os.environ.get(ENV_MARKER, "instructor"),
        help="採点者の識別子。blind 採点に記録される",
    )
    parser.add_argument("--host", default="127.0.0.1", help="認証が無いので既定は localhost のみ")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    console = Console(ReviewStore(args.golden), args.profiles, marker=args.marker)
    print(f"レビュー対象: {args.golden}")
    print(f"採点者: {args.marker}")
    print("採点は aijudge-grade が行います（このコンソールは採点しません）")
    print(f"→ http://{args.host}:{args.port}/")
    uvicorn.run(create_app(console), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
