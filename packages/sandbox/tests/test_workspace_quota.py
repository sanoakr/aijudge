"""作業域の合計サイズに上限を掛ける（#430）。

`--ulimit=fsize` は 1 ファイルの上限なので、ファイルを大量に作れば合計は
いくらでも増える。上限を超えたら中身を消して失敗にする。
"""

from __future__ import annotations

import sys
from pathlib import Path

from aijudge_sandbox import ExecRequest, Limits
from aijudge_sandbox.base import LocalSandboxBase

MIB = 1024 * 1024


class Python(LocalSandboxBase):
    def wrap(self, argv: list[str], request: ExecRequest, workdir: Path):
        return [sys.executable, "-c", *argv], {}


def test_a_run_that_fills_the_workspace_fails_and_leaves_nothing() -> None:
    writer = "\n".join(
        [
            "for i in range(4):",
            "    open(f'f{i}', 'wb').write(b'x' * 1024 * 1024)",
        ]
    )
    with Python().workspace() as workspace:
        workspace.write("main.c", "int main(void){return 0;}")
        result = workspace.run(ExecRequest(argv=(writer,), limits=Limits(workspace_bytes=2 * MIB)))
        assert result.workspace_exceeded
        assert not result.ok
        assert "cleared" in result.stderr
        assert list(workspace.path.iterdir()) == []


def test_a_normal_run_keeps_its_files() -> None:
    with Python().workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("open('out', 'w').write('ok')",)))
        assert result.ok
        assert not result.workspace_exceeded
        assert (workspace.path / "out").read_text() == "ok"
