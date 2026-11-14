"""ホストが読めない・消せないディレクトリを作る提出（#430 の上限の穴）。

以前は、作業域の合計サイズを数える `os.walk` が列挙できないディレクトリを
黙って飛ばし、後始末の `rmtree` も失敗を握りつぶしていた。提出が
`mkdir("d", 0700)` → 書く → `chmod("d", 0)` とすれば、上限をすり抜けたうえに
作業域（`aijudge-ws-*`）がホストに残った。

ここはホストと同じ uid で動かす（コンテナ不要）。別 uid（docker の 65534）の
場合は `test_container.py` が確かめる。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from aijudge_sandbox import ExecRequest, Limits
from aijudge_sandbox.base import LocalSandboxBase, _discard, grant_owner_access

MIB = 1024 * 1024

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory permissions, so nothing is unreadable"
)

# d/ に 4 MiB を書いてから、d を誰も読めないようにする。
HIDE_FOUR_MIB = "\n".join(
    [
        "import os",
        "os.mkdir('d', 0o700)",
        "for i in range(4):",
        "    open(f'd/f{i}', 'wb').write(b'x' * 1024 * 1024)",
        "os.chmod('d', 0)",
    ]
)

# 上限より十分小さい中身を、0000 のディレクトリの下の 0000 のディレクトリに隠す。
HIDE_A_LITTLE = "\n".join(
    [
        "import os",
        "os.makedirs('d/e', 0o700)",
        "open('d/e/small', 'w').write('x')",
        "os.chmod('d/e', 0)",
        "os.chmod('d', 0)",
    ]
)


class Python(LocalSandboxBase):
    def wrap(self, argv: list[str], request: ExecRequest, workdir: Path):
        return [sys.executable, "-c", *argv], {}


class CannotReclaim(Python):
    """権限を取り返せないバックエンド（docker で取り返しのコンテナが失敗した場合）。"""

    def reclaim(self, directory: Path) -> None:
        return None


def test_a_hidden_directory_still_counts_toward_the_limit() -> None:
    with Python().workspace() as workspace:
        path = workspace.path
        result = workspace.run(
            ExecRequest(argv=(HIDE_FOUR_MIB,), limits=Limits(workspace_bytes=2 * MIB))
        )
        assert result.workspace_exceeded
        assert not result.ok
        assert list(path.iterdir()) == []
    assert not path.exists()


def test_a_small_hidden_directory_is_measured_and_removed_afterwards() -> None:
    with Python().workspace() as workspace:
        path = workspace.path
        result = workspace.run(ExecRequest(argv=(HIDE_A_LITTLE,)))
        assert result.ok
        assert not result.workspace_exceeded
    assert not path.exists()


def test_a_workspace_that_cannot_be_measured_counts_as_exceeded(caplog) -> None:
    """取り返せなければ上限超え（fail closed）。残ったものは ERROR で記録する。"""
    caplog.set_level(logging.ERROR, logger="aijudge_sandbox.base")
    path: Path | None = None
    try:
        with CannotReclaim().workspace() as workspace:
            path = workspace.path
            result = workspace.run(ExecRequest(argv=(HIDE_A_LITTLE,)))
            assert result.workspace_exceeded
            assert "could not be measured" in result.stderr
        assert path.exists()
        assert any("could not remove workspace" in r.getMessage() for r in caplog.records)
    finally:
        if path is not None and path.exists():
            grant_owner_access(path)
            shutil.rmtree(path)


def test_discarding_a_workspace_reclaims_permissions_before_removing_it() -> None:
    """作業域を丸ごと消す処理が、権限を取り返してからもう一度消すこと。

    計測の側（`run`）も取り返すので、通常の流れでは消す側の取り返しは使われない。
    だが消す処理は単独でも呼ばれうる（実行の外で作業域が残ったとき）。そこで
    権限が閉じたまま消せずに残ると、残骸（`aijudge-ws-*`）を誰も掃除しないので、
    置き場所のファイルシステムが静かに埋まる。取り返す段を外すと、この件が落ちる。
    """
    path = Path(tempfile.mkdtemp(prefix="aijudge-ws-"))
    (path / "d" / "e").mkdir(parents=True)
    (path / "d" / "e" / "small").write_text("x", encoding="utf-8")
    os.chmod(path / "d" / "e", 0)
    os.chmod(path / "d", 0)
    try:
        _discard(path, grant_owner_access)
        assert not path.exists(), "権限を取り返さずに消そうとして、作業域が残った"
    finally:
        if path.exists():
            grant_owner_access(path)
            shutil.rmtree(path)
