"""`aijudge-web` — 学習者向け Web アプリを起動する。

    uv run aijudge-web --create-schema      # 開発時（スキーマを作る）
    uv run aijudge-web --host 0.0.0.0
    uv run aijudge-web --workers 4          # 締切集中・動画アップロードの同時数対策

既定は `127.0.0.1` にしか bind しない。他の端末から使うなら
`tailscale serve` を前に立てる（`docs/RUNNING.md`）。TLS 終端が
`X-Forwarded-Proto: https` を付けるので、セッション Cookie に `Secure` が
自動で付く。プロキシを立てずに直接 bind する場合は平文になるので、
その経路を学生に配らないこと。

**`--workers` を 2 以上にすると uvicorn は子プロセスを fork する。**
子はこのモジュールを import し直して `make_app()` を呼ぶので、設定は
**環境変数からしか渡らない**（argparse の値は届かない）。親が解決した値を
`os.environ` へ書き出してから起動する。
"""

from __future__ import annotations

import argparse
import os
import sys
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
ENV_MAX_CONCURRENT_VIDEO = "AIJUDGE_MAX_CONCURRENT_VIDEO"
# 待ち時間の概算に使う AI ワーカー数のヒント（採点を止める値ではない）。
ENV_AI_WORKERS = "AIJUDGE_AI_WORKERS"
ENV_PROFILES_DIR = "AIJUDGE_PROFILES_DIR"
ENV_WORKERS = "AIJUDGE_WEB_WORKERS"
# 教員コンソールの場所（#103）。役割はコースごとなので、TA や教員として
# 取っているコースの行から渡す。**空でも動く。**
ENV_CONSOLE_URL = "AIJUDGE_CONSOLE_URL"
DEFAULT_ARTIFACT_DIR = Path.home() / ".aijudge" / "artifacts"
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MAX_CONCURRENT_VIDEO = 4
DEFAULT_AI_WORKERS = 1


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
            max_concurrent_video=args.max_concurrent_video,
            ai_workers=args.ai_workers,
            console_url=args.console_url,
            console_port=args.console_port,
        )
    )


def make_app():
    """環境変数だけからアプリを組む（`--workers > 1` の子プロセス用）。

    親が `_export_env` で書き出した値を読む。`--create-schema` は開発用なので
    ここでは常に無効（複数ワーカーは本番構成）。
    """
    video = os.environ.get(ENV_VIDEO_DIR)
    ns = argparse.Namespace(
        database_url=os.environ.get(ENV_DATABASE_URL),
        artifacts=Path(os.environ.get(ENV_ARTIFACT_DIR, DEFAULT_ARTIFACT_DIR)).expanduser(),
        video_dir=Path(video).expanduser() if video else None,
        max_upload_bytes=int(os.environ.get(ENV_MAX_UPLOAD_BYTES, DEFAULT_MAX_UPLOAD_BYTES)),
        max_video_bytes=int(os.environ.get(ENV_MAX_VIDEO_BYTES, DEFAULT_MAX_VIDEO_BYTES)),
        max_concurrent_video=int(
            os.environ.get(ENV_MAX_CONCURRENT_VIDEO, DEFAULT_MAX_CONCURRENT_VIDEO)
        ),
        ai_workers=int(os.environ.get(ENV_AI_WORKERS, DEFAULT_AI_WORKERS)),
        profiles=Path(os.environ.get(ENV_PROFILES_DIR, REPO_ROOT / "subjects")),
        console_url=os.environ.get(ENV_CONSOLE_URL, ""),
        console_port=int(os.environ.get("AIJUDGE_CONSOLE_PORT", 8765)),
        create_schema=False,
    )
    return build_app(ns)


def _export_env(args: argparse.Namespace) -> None:
    """解決済みの設定を子プロセスへ渡すため環境変数に焼き付ける。"""
    if args.database_url:
        os.environ[ENV_DATABASE_URL] = args.database_url
    os.environ[ENV_ARTIFACT_DIR] = str(args.artifacts)
    if args.video_dir:
        os.environ[ENV_VIDEO_DIR] = str(args.video_dir)
    os.environ[ENV_MAX_UPLOAD_BYTES] = str(args.max_upload_bytes)
    os.environ[ENV_MAX_VIDEO_BYTES] = str(args.max_video_bytes)
    os.environ[ENV_MAX_CONCURRENT_VIDEO] = str(args.max_concurrent_video)
    os.environ[ENV_AI_WORKERS] = str(args.ai_workers)
    os.environ[ENV_PROFILES_DIR] = str(args.profiles)
    os.environ[ENV_CONSOLE_URL] = args.console_url
    os.environ["AIJUDGE_CONSOLE_PORT"] = str(args.console_port)


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
        default=int(os.environ.get(ENV_MAX_UPLOAD_BYTES, DEFAULT_MAX_UPLOAD_BYTES)),
        help="通常提出 1 ファイルの上限（既定 20 MiB）",
    )
    parser.add_argument(
        "--max-video-bytes",
        type=int,
        default=int(os.environ.get(ENV_MAX_VIDEO_BYTES, DEFAULT_MAX_VIDEO_BYTES)),
        help="動画 1 ファイルの上限（既定 5 GiB）",
    )
    parser.add_argument(
        "--max-concurrent-video",
        type=int,
        default=int(os.environ.get(ENV_MAX_CONCURRENT_VIDEO, DEFAULT_MAX_CONCURRENT_VIDEO)),
        help="1 プロセスで同時に受ける動画アップロード数（既定 4）。超過は 429",
    )
    parser.add_argument(
        "--ai-workers",
        type=int,
        default=int(os.environ.get(ENV_AI_WORKERS, DEFAULT_AI_WORKERS)),
        help="AI ワーカー数のヒント（採点待ち時間の概算に使うだけ・既定 1）",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path(os.environ.get(ENV_PROFILES_DIR, REPO_ROOT / "subjects")),
    )
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
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get(ENV_WORKERS, 1)),
        help="uvicorn ワーカープロセス数（既定 1）。締切集中や動画の同時アップロードで増やす",
    )
    parser.add_argument("--create-schema", action="store_true", help="開発用")
    args = parser.parse_args(argv)

    print(f"→ http://{args.host}:{args.port}/  (workers={args.workers})")
    print("採点は aijudge-worker が行います（別プロセスで起動してください）")

    if args.workers > 1:
        if args.create_schema:
            print("--workers > 1 と --create-schema は併用できません（本番構成）", file=sys.stderr)
            return 2
        if (args.database_url or "").startswith("sqlite"):
            print(
                "警告: SQLite で複数ワーカーは書き込みが競合します。PostgreSQL にしてください。",
                file=sys.stderr,
            )
        # スキーマ作成が要るなら先に単独で済ませておく前提。子は env から組む。
        _export_env(args)
        uvicorn.run(
            "aijudge_studentweb.cli:make_app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            factory=True,
            log_level="warning",
        )
    else:
        uvicorn.run(build_app(args), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
