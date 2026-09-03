"""`aijudge-review` — 教員レビューコンソールを起動する。

    uv run aijudge-review

**採点は別プロセス**（`aijudge-worker`）。レビューは採点の前提条件では
なく、採点が届いた提出を教員が確定させる作業（ADR 0007）。

既定は `127.0.0.1` にしか bind しない。他の端末から使うなら
`tailscale serve` を前に立てる（`docs/RUNNING.md`）。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from aijudge_persistence import ENV_DATABASE_URL, Database, ObservationFileStore
from aijudge_submission import FilesystemArtifactStore

from .app import Console, create_app

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_ARTIFACT_DIR = "AIJUDGE_ARTIFACT_DIR"
ENV_OBSERVATION_DIR = "AIJUDGE_OBSERVATION_DIR"
# 学習者アプリの場所（#103）。同じ人が「A では学習者・B では教員」になるので、
# 採点しないコースの行から学習者側へ渡す。**空でも動く。**
ENV_LEARNER_URL = "AIJUDGE_LEARNER_URL"
DEFAULT_ARTIFACT_DIR = Path.home() / ".aijudge" / "artifacts"
DEFAULT_OBSERVATION_DIR = Path.home() / ".aijudge" / "observations"


def build_console(args: argparse.Namespace) -> Console:
    database = Database.connect(args.database_url, create=args.create_schema)
    return Console(
        database,
        FilesystemArtifactStore(args.artifacts),
        profiles_dir=args.profiles,
        observations=ObservationFileStore(args.observations),
        learner_url=args.learner_url,
        learner_port=args.learner_port,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aijudge-review", description="教員レビューコンソール")
    parser.add_argument("--database-url", default=os.environ.get(ENV_DATABASE_URL))
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get(ENV_ARTIFACT_DIR, DEFAULT_ARTIFACT_DIR)).expanduser(),
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path(os.environ.get(ENV_OBSERVATION_DIR, DEFAULT_OBSERVATION_DIR)).expanduser(),
        help="観測レコードの置き場所（測定用。無くてもレビューは動く）",
    )
    parser.add_argument("--profiles", type=Path, default=REPO_ROOT / "subjects")
    parser.add_argument(
        "--learner-url",
        default=os.environ.get(ENV_LEARNER_URL, ""),
        help="学習者アプリの場所（例 https://aijudge.example.jp）。"
        "空なら、開いているホスト名のまま --learner-port へ渡す",
    )
    parser.add_argument(
        "--learner-port",
        type=int,
        default=int(os.environ.get("AIJUDGE_LEARNER_PORT", 8080)),
        help="学習者アプリのポート（--learner-url が空のときに使う）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="既定は localhost のみ")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--create-schema", action="store_true", help="開発用")
    args = parser.parse_args(argv)

    console = build_console(args)
    print(f"→ http://{args.host}:{args.port}/")
    print("採点は aijudge-worker が行います（このコンソールは採点しません）")
    uvicorn.run(create_app(console), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
