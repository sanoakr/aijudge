"""`aijudge-web` — 学習者向け Web アプリを起動する。

    uv run aijudge-web --create-schema      # 開発時（スキーマを作る）
    uv run aijudge-web --host 0.0.0.0

.. warning::

   セッション Cookie の `secure` は既定で偽。**TLS 終端の前に真にすること**
   （`app.py` の該当箇所）。平文 HTTP でセッションを流すと、同じ
   ネットワークに居る誰でも他人として提出できる。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from aijudge_persistence import ENV_DATABASE_URL, Database
from aijudge_submission import FilesystemArtifactStore

from .app import StudentApp, create_app

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_ARTIFACT_DIR = "AIJUDGE_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR = Path.home() / ".aijudge" / "artifacts"


def build_app(args: argparse.Namespace):
    database = Database.connect(args.database_url, create=args.create_schema)
    return create_app(
        StudentApp(
            database,
            FilesystemArtifactStore(args.artifacts),
            profiles_dir=args.profiles,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aijudge-web", description="学習者向け Web アプリ")
    parser.add_argument("--database-url", default=os.environ.get(ENV_DATABASE_URL), help="接続先")
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get(ENV_ARTIFACT_DIR, DEFAULT_ARTIFACT_DIR)).expanduser(),
        help="提出物の置き場所",
    )
    parser.add_argument("--profiles", type=Path, default=REPO_ROOT / "subjects")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--create-schema", action="store_true", help="開発用")
    args = parser.parse_args(argv)

    app = build_app(args)
    print(f"→ http://{args.host}:{args.port}/")
    print("採点は aijudge-worker が行います（別プロセスで起動してください）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
