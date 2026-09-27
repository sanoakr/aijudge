"""包んだ先の起動にかかる時間を、提出物の持ち時間に数えない（#489）。

docker は実時間の上限をコンテナの**中**で掛け（`timeout`）、外のクライアントは
上限に起動の余裕を足して待つ。以前は外で上限を掛けていたので、起動が遅いと
正しい提出が時間切れになった（CI で同じ模範解答が 0.8 と 1.0 に割れた）。

ここではコンテナ無しで、「起動の遅い包み方」を Python で模して固定する。
実際のコンテナでの振る舞いは `test_container.py` が見る。
"""

from __future__ import annotations

import sys
from pathlib import Path

from aijudge_sandbox import ExecRequest, ExecResult, Limits
from aijudge_sandbox.backends import TIMEOUT_EXIT_CODE, DockerSandbox, _lacks_timeout
from aijudge_sandbox.base import LocalSandboxBase

#: 模した起動の遅さ。持ち時間（`WALL`）より長くする。
STARTUP_SECONDS = 1.5
WALL = 1.0


class SlowToStart(LocalSandboxBase):
    """起動に `STARTUP_SECONDS` かかってから argv を動かす包み方。"""

    def __init__(self, allowance: float) -> None:
        self.startup_allowance_seconds = allowance

    def wrap(self, argv: list[str], request: ExecRequest, workdir: Path):
        code = f"import time, subprocess, sys; time.sleep({STARTUP_SECONDS}); "
        code += "sys.exit(subprocess.call(sys.argv[1:]))"
        return [sys.executable, "-c", code, *argv], {}

    def timed_out_inside(self, code: int) -> bool:
        return DockerSandbox.timed_out_inside(self, code)  # type: ignore[arg-type]


def _quick_program() -> ExecRequest:
    return ExecRequest(
        argv=(sys.executable, "-c", "print('ok')"),
        limits=Limits(cpu_seconds=5, wall_seconds=WALL),
    )


def test_a_slow_start_does_not_eat_the_time_limit() -> None:
    with SlowToStart(allowance=10.0).workspace() as workspace:
        result = workspace.run(_quick_program())
    assert result.ok, result
    assert not result.timed_out
    assert result.stdout.strip() == "ok"


def test_without_the_allowance_a_slow_start_reads_as_a_timeout() -> None:
    """直す前の振る舞い。余裕が無いと、正しい提出が時間切れになる。"""
    with SlowToStart(allowance=0.0).workspace() as workspace:
        result = workspace.run(_quick_program())
    assert result.timed_out


def test_the_inner_limit_is_read_as_a_timeout() -> None:
    """中の `timeout` が止めたら（124）、時間切れとして分類する。"""
    with SlowToStart(allowance=10.0).workspace() as workspace:
        result = workspace.run(
            ExecRequest(
                argv=(sys.executable, "-c", f"import sys; sys.exit({TIMEOUT_EXIT_CODE})"),
                limits=Limits(cpu_seconds=5, wall_seconds=WALL),
            )
        )
    assert result.timed_out
    assert not result.ok
    assert result.signal_name is None


def test_an_image_without_timeout_is_recognised() -> None:
    missing = ExecResult(
        exit_code=127,
        stderr='docker: Error response from daemon: exec: "timeout": executable file '
        "not found in $PATH",
    )
    assert _lacks_timeout(missing)
    assert not _lacks_timeout(ExecResult(exit_code=127, stderr="cat: not found"))
    assert not _lacks_timeout(ExecResult(exit_code=0, stdout="mounted"))
