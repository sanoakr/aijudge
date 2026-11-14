"""`LocalWorkspace.write` の脱出検査が、リンクの先と絶対パスを見ていること（#703）。

`test_sandbox.py` は名前に `..` を含む 1 例だけで、検査を「名前に `..` があれば
断る」という文字列の判定に単純化しても全件が通った（#703 の変異）。

検査が実際に効くのは、**提出物が走ったあとにホストが同じ作業域へ書く**経路
（`CFunctionCheck._run_checks` が検査用ソースごとに `workspace.write` を繰り返す）。
提出物は作業域の中に何でも置ける ── 作業域の外を指すシンボリックリンクも。
そこへホストの権限で書けば、作業域の外のファイルが書き換わる。

隔離なしのバックエンドを使うのは、作業域の作り方がどのバックエンドでも同じ
（`LocalWorkspace`）で、ここで見たいのはホスト側の書き込みだけだから。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from aijudge_sandbox import UnsafeLocalSandbox
from aijudge_sandbox.base import LocalWorkspace

ORIGINAL = "host file, must not change"


@pytest.fixture
def workspace() -> Iterator[LocalWorkspace]:
    with UnsafeLocalSandbox(allow_unsafe=True).workspace() as opened:
        assert isinstance(opened, LocalWorkspace)
        yield opened


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """作業域の外。提出物のリンクが指す先を模す。"""
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "victim.txt").write_text(ORIGINAL, encoding="utf-8")
    return directory


def test_a_link_to_a_file_outside_is_not_written_through(workspace, outside) -> None:
    victim = outside / "victim.txt"
    os.symlink(victim, workspace.path / "prog.c")
    with pytest.raises(ValueError, match="escapes the workspace"):
        workspace.write("prog.c", "pwned")
    assert victim.read_text(encoding="utf-8") == ORIGINAL


def test_a_link_to_a_directory_outside_is_not_written_into(workspace, outside) -> None:
    os.symlink(outside, workspace.path / "link")
    with pytest.raises(ValueError, match="escapes the workspace"):
        workspace.write("link/planted.txt", "pwned")
    assert not (outside / "planted.txt").exists()


def test_a_dangling_link_to_outside_is_not_written_through(workspace, outside) -> None:
    """まだ無いファイルを指すリンク。書けば作業域の外に新しくファイルができる。"""
    target = outside / "created.txt"
    os.symlink(target, workspace.path / "check.c")
    with pytest.raises(ValueError, match="escapes the workspace"):
        workspace.write("check.c", "pwned")
    assert not target.exists()


def test_an_absolute_path_is_refused(workspace, outside) -> None:
    """`Path / "/abs"` は絶対パスの方を採る。名前に `..` が無くても外へ出る。"""
    target = outside / "abs.txt"
    with pytest.raises(ValueError, match="escapes the workspace"):
        workspace.write(str(target), "pwned")
    assert not target.exists()


def test_a_link_that_stays_inside_is_allowed(workspace) -> None:
    """上の試験が「リンクならいつも断る」ことで通っていないことの確かめ。"""
    (workspace.path / "sub").mkdir()
    os.symlink(workspace.path / "sub", workspace.path / "alias")
    written = workspace.write("alias/ok.txt", "fine")
    assert (workspace.path / "sub" / "ok.txt").read_text(encoding="utf-8") == "fine"
    assert written == workspace.path / "alias" / "ok.txt"
