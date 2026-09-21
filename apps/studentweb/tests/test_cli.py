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
    cli.ENV_MAX_VIDEO_BYTES_WITHOUT_DEADLINE,
    cli.ENV_MAX_CONCURRENT_VIDEO,
    cli.ENV_AI_WORKERS,
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
    monkeypatch.setenv(cli.ENV_MAX_VIDEO_BYTES_WITHOUT_DEADLINE, "654")
    monkeypatch.setenv("AIJUDGE_CONSOLE_PORT", "9765")

    app = cli.make_app()
    assert isinstance(app, FastAPI)
    state = app.state.aijudge
    assert state.max_video_bytes == 123456
    assert state.max_video_bytes_without_deadline == 654
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
        max_video_bytes_without_deadline=4,
        max_concurrent_video=3,
        ai_workers=5,
        profiles=tmp_path / "subjects",
        console_url="https://x/teach",
        console_port=8443,
    )
    cli._export_env(ns)
    import os

    assert os.environ[cli.ENV_VIDEO_DIR] == str(tmp_path / "v")
    assert os.environ[cli.ENV_MAX_VIDEO_BYTES] == "2"
    # 締切の無い課題の上限も子プロセスへ渡す（ADR 0020）── ここが落ちると、
    # `--workers 2` のときだけ 256 MiB の既定に戻る。
    assert os.environ[cli.ENV_MAX_VIDEO_BYTES_WITHOUT_DEADLINE] == "4"
    assert os.environ[cli.ENV_MAX_CONCURRENT_VIDEO] == "3"
    assert os.environ[cli.ENV_AI_WORKERS] == "5"
    assert os.environ["AIJUDGE_CONSOLE_PORT"] == "8443"


def test_workers_gt_1_with_create_schema_is_rejected(capsys) -> None:
    rc = cli.main(
        ["--workers", "2", "--create-schema", "--database-url", "sqlite+pysqlite:///:memory:"]
    )
    assert rc == 2
    assert "併用できません" in capsys.readouterr().err


def test_unwritable_video_dir_names_the_variable(monkeypatch, tmp_path: Path) -> None:
    """`AIJUDGE_VIDEO_DIR` に書けないとき、traceback ではなく変数名で落ちる。

    v1.12.0 で運用機が踏んだ ── systemd の `ReadWritePaths` の外に動画置き場が
    あり、起動時の `_incomplete/` 作成が `Read-only file system` で死んだ。
    原因は 40 行の traceback の最後の 1 行にしか無かった。
    """
    import pytest

    def refuse(self, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(SystemExit) as info:
        cli._open_video_dir(tmp_path / "video")
    message = str(info.value)
    assert cli.ENV_VIDEO_DIR in message
    assert str(tmp_path / "video") in message
    assert "Read-only file system" in message
    assert "ReadWritePaths" in message


def test_no_video_dir_means_no_store() -> None:
    assert cli._open_video_dir(None) == (None, None)
