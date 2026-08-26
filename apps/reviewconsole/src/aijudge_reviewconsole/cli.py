"""`aijudge-review` — 教員レビューコンソールを起動する。

    uv run aijudge-review --golden ~/.aijudge/golden --marker sano

認証は無い。既定で 127.0.0.1 にしか bind しないのはそのため。
学内に公開するなら先に S1（Identity）に載せること。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .app import ENV_GOLDEN_DIR, ENV_MARKER, Console, create_app
from .store import ReviewStore

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_GOLDEN_DIR = Path.home() / ".aijudge" / "golden"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aijudge-review", description="教員レビューコンソール")
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path(os.environ.get(ENV_GOLDEN_DIR, DEFAULT_GOLDEN_DIR)).expanduser(),
        help="レビュー対象とゴールデンセットの場所",
    )
    parser.add_argument(
        "--profiles", type=Path, default=REPO_ROOT / "subjects", help="科目プロファイルの場所"
    )
    parser.add_argument(
        "--marker",
        default=os.environ.get(ENV_MARKER, "instructor"),
        help="採点者の識別子。ゴールデンセットに記録される",
    )
    parser.add_argument("--host", default="127.0.0.1", help="認証が無いので既定は localhost のみ")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    console = Console(ReviewStore(args.golden), args.profiles, marker=args.marker)
    print(f"レビュー対象: {args.golden}")
    print(f"採点者: {args.marker}")
    print(f"→ http://{args.host}:{args.port}/")
    uvicorn.run(create_app(console), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
