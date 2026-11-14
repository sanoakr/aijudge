"""`docker run` に渡す隔離と上限の引数を固定する（#703）。

`test_tool_sandbox.py` は ENTRYPOINT と `timeout` の形だけを見ていて、プロセス数・
メモリ・ネットワーク・権限の引数が入っていることを確かめるテストは無かった。
`--pids-limit` を消しても `test_container.py` の fork bomb 試験は通りうる
（CPU 時間の上限やメモリの上限でも「殺された」になる）。ADR 0006 が実提出を
通す前提とした性質なので、組み立ての段階でも固定する。

**オプションはイメージ名より前にあること**も見る。後ろに置くと docker の
オプションではなく、コンテナの中で動くコマンドの引数として読まれ、黙って
効かない。

docker は呼ばない（`DockerSandbox.__new__` で、確かめの起動を飛ばして作る）。
実際に効くことは `test_container_process_limit.py` と `test_container.py` が見る。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_sandbox import ExecRequest, Limits
from aijudge_sandbox.backends import DEFAULT_IMAGE, DockerSandbox

LIMITS = Limits(processes=16, memory_bytes=128 * 1024 * 1024)


def _unverified(runtime: str | None) -> DockerSandbox:
    """docker を呼ばずに作る（引数の組み立てだけを見る）。"""
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox._binary = "/usr/bin/docker"
    sandbox._image = DEFAULT_IMAGE
    sandbox._runtime = runtime
    sandbox._clear_entrypoint = False
    return sandbox


def _options(runtime: str | None = None) -> list[str]:
    """イメージ名より前（docker が自分のオプションとして読む範囲）。"""
    sandbox = _unverified(runtime)
    command, _ = sandbox.wrap(
        ["./prog"],
        ExecRequest(argv=("./prog",), limits=LIMITS),
        Path("/tmp/w"),
    )
    return command[: command.index(DEFAULT_IMAGE)]


# id に `runsc` を含めない。CI の本体の段は `-k "not runsc"` で流すので、含めると
# docker の要らないこの試験まで外れる。
@pytest.mark.parametrize("runtime", [None, "runsc"], ids=["runc", "gvisor"])
@pytest.mark.parametrize(
    "flag",
    [
        "--pids-limit=16",
        f"--memory={128 * 1024 * 1024}",
        # swap を許すとメモリ上限が意味を失う。
        f"--memory-swap={128 * 1024 * 1024}",
        "--network=none",
        "--read-only",
        "--user=65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ],
)
def test_isolation_and_limits_are_docker_options(runtime: str | None, flag: str) -> None:
    assert flag in _options(runtime)


def test_the_process_limit_follows_the_request() -> None:
    """固定の大きな値に置き換わっていないこと（値が上限から来ていること）。"""
    sandbox = _unverified(None)
    command, _ = sandbox.wrap(
        ["./prog"],
        ExecRequest(argv=("./prog",), limits=Limits(processes=7)),
        Path("/tmp/w"),
    )
    assert [part for part in command if part.startswith("--pids-limit")] == ["--pids-limit=7"]


def test_no_option_widens_what_the_submission_can_do() -> None:
    """特権・ホストの名前空間・追加の権限を渡していないこと。"""
    options = _options()
    for forbidden in ("--privileged", "--cap-add", "--network=host", "--pid=host", "--ipc=host"):
        assert not any(part.startswith(forbidden) for part in options), forbidden


def test_the_gvisor_runtime_is_passed_as_a_docker_option() -> None:
    assert "--runtime=runsc" in _options("runsc")
    assert not any(part.startswith("--runtime") for part in _options(None))
