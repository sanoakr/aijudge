"""時間切れのあと、包んだ先のランタイムを後始末に回す（#410）。

docker では `docker run` のクライアントを殺してもコンテナは生き残る。
ここではコンテナ無しで「時間切れなら `release` が包んだ argv で呼ばれる」
ことだけを固定する（実際に消えることは `test_container.py` が見る）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from aijudge_sandbox import ExecRequest, Limits
from aijudge_sandbox.base import LocalSandboxBase


class Recording(LocalSandboxBase):
    """argv をそのまま Python で動かし、後始末の呼び出しを記録する。"""

    def __init__(self) -> None:
        self.released: list[list[str]] = []

    def wrap(self, argv: list[str], request: ExecRequest, workdir: Path):
        return [sys.executable, "-c", *argv], {}

    def release(self, argv: list[str]) -> None:
        self.released.append(argv)


def test_a_timed_out_run_is_released() -> None:
    backend = Recording()
    with backend.workspace() as workspace:
        result = workspace.run(
            ExecRequest(
                argv=("import time; time.sleep(30)",),
                limits=Limits(cpu_seconds=5, wall_seconds=0.5),
            )
        )
    assert result.timed_out
    assert backend.released == [[sys.executable, "-c", "import time; time.sleep(30)"]]


def test_a_run_that_finishes_is_not_released() -> None:
    backend = Recording()
    with backend.workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("print('ok')",)))
    assert result.ok
    assert backend.released == []
