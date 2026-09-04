"""`--workers > 1` の起動経路（env 経由でアプリを組む）を固定する。

uvicorn は `--workers` を増やすと子プロセスを fork し、子は argparse の値を
受け取れない。親が `_export_env` で環境変数へ焼き付け、子は `make_app` で
そこから組む。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from aijudge_studentweb import cli

_ENV_KEYS = (
    cli.ENV_DATABASE_URL,
    cli.ENV_ARTIFACT_DIR,
    cli.ENV_VIDEO_DIR,
    cli.ENV_MAX_UPLOAD_BYTES,
    cli.ENV_MAX_VIDEO_BYTES,
    cli.ENV_PROFILES_DIR,
    cli.ENV_CONSOLE_URL,
    "AIJUDGE_CONSOLE_PORT",
)


def test_make_app_builds_from_env(monkeypatch, tmp_path: Path) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(cli.ENV_DATABASE_URL, "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv(cli.ENV_ARTIFACT_DIR, str(tmp_path / "artifacts"))
    monkeypatch.setenv(cli.ENV_VIDEO_DIR, str(tmp_path / "video"))
    monkeypatch.setenv(cli.ENV_MAX_VIDEO_BYTES, "123456")
    monkeypatch.setenv("AIJUDGE_CONSOLE_PORT", "9765")

    app = cli.make_app()
    assert isinstance(app, FastAPI)
    state = app.state.aijudge
    assert state.max_video_bytes == 123456
    assert state.video_store is not None
    assert state.console_port == 9765


def test_export_env_reflects_resolved_args(monkeypatch, tmp_path: Path) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    import argparse

    ns = argparse.Namespace(
        database_url="postgresql+psycopg://aijudge@/aijudge",
        artifacts=tmp_path / "a",
        video_dir=tmp_path / "v",
        max_upload_bytes=1,
        max_video_bytes=2,
        profiles=tmp_path / "subjects",
        console_url="https://x/teach",
        console_port=8443,
    )
    cli._export_env(ns)
    import os

    assert os.environ[cli.ENV_VIDEO_DIR] == str(tmp_path / "v")
    assert os.environ[cli.ENV_MAX_VIDEO_BYTES] == "2"
    assert os.environ["AIJUDGE_CONSOLE_PORT"] == "8443"


def test_workers_gt_1_with_create_schema_is_rejected(capsys) -> None:
    rc = cli.main(
        ["--workers", "2", "--create-schema", "--database-url", "sqlite+pysqlite:///:memory:"]
    )
    assert rc == 2
    assert "併用できません" in capsys.readouterr().err
