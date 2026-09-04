"""`aijudge-web` — 学習者向け Web アプリを起動する。

    uv run aijudge-web --create-schema      # 開発時（スキーマを作る）
    uv run aijudge-web --host 0.0.0.0

既定は `127.0.0.1` にしか bind しない。他の端末から使うなら
`tailscale serve` を前に立てる（`docs/RUNNING.md`）。TLS 終端が
`X-Forwarded-Proto: https` を付けるので、セッション Cookie に `Secure` が
自動で付く。プロキシを立てずに直接 bind する場合は平文になるので、
その経路を学生に配らないこと。
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
# 動画の置き場所（通常の提出物とは別ディスクに置ける）。空なら動画提出は 501。
ENV_VIDEO_DIR = "AIJUDGE_VIDEO_DIR"
ENV_MAX_UPLOAD_BYTES = "AIJUDGE_MAX_UPLOAD_BYTES"
ENV_MAX_VIDEO_BYTES = "AIJUDGE_MAX_VIDEO_BYTES"
# 教員コンソールの場所（#103）。役割はコースごとなので、TA や教員として
# 取っているコースの行から渡す。**空でも動く。**
ENV_CONSOLE_URL = "AIJUDGE_CONSOLE_URL"
DEFAULT_ARTIFACT_DIR = Path.home() / ".aijudge" / "artifacts"


def build_app(args: argparse.Namespace):
    database = Database.connect(args.database_url, create=args.create_schema)
    video_store = FilesystemArtifactStore(args.video_dir) if args.video_dir else None
    return create_app(
        StudentApp(
            database,
            FilesystemArtifactStore(args.artifacts),
            profiles_dir=args.profiles,
            video_store=video_store,
            max_upload_bytes=args.max_upload_bytes,
            max_video_bytes=args.max_video_bytes,
            console_url=args.console_url,
            console_port=args.console_port,
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
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=(
            Path(os.environ[ENV_VIDEO_DIR]).expanduser() if os.environ.get(ENV_VIDEO_DIR) else None
        ),
        help="動画の置き場所（別ルート submit-video 用）。未指定なら動画提出は無効",
    )
    parser.add_argument(
        "--max-upload-bytes",
        type=int,
        default=int(os.environ.get(ENV_MAX_UPLOAD_BYTES, 20 * 1024 * 1024)),
        help="通常提出 1 ファイルの上限（既定 20 MiB）",
    )
    parser.add_argument(
        "--max-video-bytes",
        type=int,
        default=int(os.environ.get(ENV_MAX_VIDEO_BYTES, 5 * 1024 * 1024 * 1024)),
        help="動画 1 ファイルの上限（既定 5 GiB）",
    )
    parser.add_argument("--profiles", type=Path, default=REPO_ROOT / "subjects")
    parser.add_argument(
        "--console-url",
        default=os.environ.get(ENV_CONSOLE_URL, ""),
        help="教員コンソールの場所（例 https://aijudge.example.jp:8765）。"
        "空なら、開いているホスト名のまま --console-port へ渡す",
    )
    parser.add_argument(
        "--console-port",
        type=int,
        default=int(os.environ.get("AIJUDGE_CONSOLE_PORT", 8765)),
        help="教員コンソールのポート（--console-url が空のときに使う）",
    )
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
