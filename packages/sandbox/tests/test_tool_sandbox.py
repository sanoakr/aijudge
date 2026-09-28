"""道具のイメージ（Dolos など）を動かす sandbox（#203）。

提出物ではなく、運用者が固定した道具をイメージごと動かす。コンテナにしか
落ちないこと、ENTRYPOINT を外すこと、`timeout` を busybox でも通る形で
渡すことを固定する。実際のコンテナでの振る舞いは `test_container.py` が見る。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from aijudge_sandbox import ExecRequest, Isolation, Limits, SandboxUnavailable, selection
from aijudge_sandbox.backends import DockerSandbox

TOOL = "ghcr.io/example/tool@sha256:" + "0" * 64


def _unverified(image: str, *, clear_entrypoint: bool) -> DockerSandbox:
    """docker を呼ばずに作る（引数の組み立てだけを見る）。"""
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox._binary = "/usr/bin/docker"
    sandbox._image = image
    sandbox._runtime = None
    sandbox._clear_entrypoint = clear_entrypoint
    return sandbox


def _argv(sandbox: DockerSandbox) -> list[str]:
    request = ExecRequest(argv=("dolos", "run"), limits=Limits(wall_seconds=120.0))
    command, _ = sandbox.wrap(["dolos", "run"], request, Path("/tmp/w"))
    return command


def test_the_entrypoint_is_cleared_just_before_the_image() -> None:
    command = _argv(_unverified(TOOL, clear_entrypoint=True))
    at = command.index(TOOL)
    assert command[at - 1] == "--entrypoint="


def test_grading_images_keep_their_entrypoint() -> None:
    assert "--entrypoint=" not in _argv(_unverified("gcc:14-bookworm", clear_entrypoint=False))


def test_the_timeout_uses_the_form_busybox_accepts() -> None:
    """`--kill-after=` は busybox が知らず、何も起動できない（Dolos のイメージで実測）。"""
    command = _argv(_unverified(TOOL, clear_entrypoint=True))
    at = command.index("timeout")
    assert command[at : at + 4] == ["timeout", "-k", "1", "120.0"]
    assert command[at + 4 :] == ["dolos", "run"]
    assert not any(part.startswith("--kill-after") for part in command)


class _Recorder:
    """`DockerSandbox` の代わり。作られ方を記録し、指定した runtime だけ失敗する。"""

    made: ClassVar[list[tuple[str, str | None, bool]]] = []
    missing: ClassVar[set[str | None]] = set()

    def __init__(self, image: str, *, runtime: str | None = None, clear_entrypoint=False):
        if runtime in self.missing:
            raise SandboxUnavailable(f"no {runtime}")
        self.made.append((image, runtime, clear_entrypoint))
        self.name = f"docker:{runtime}" if runtime else "docker"
        self.isolation = Isolation.KERNEL_ISOLATED if runtime == "runsc" else Isolation.CONTAINER


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.made = []
    _Recorder.missing = set()
    monkeypatch.setattr(selection, "DockerSandbox", _Recorder)
    monkeypatch.setenv(selection.ENV_ALLOWED_IMAGES, TOOL)
    monkeypatch.delenv(selection.ENV_BACKEND, raising=False)
    monkeypatch.delenv(selection.ENV_MINIMUM, raising=False)
    return _Recorder


def test_an_unlisted_tool_image_is_refused(recorder, monkeypatch) -> None:
    monkeypatch.setenv(selection.ENV_ALLOWED_IMAGES, "")
    with pytest.raises(SandboxUnavailable):
        selection.build_tool_sandbox(TOOL)
    assert recorder.made == []


def test_gvisor_is_preferred_and_the_entrypoint_cleared(recorder) -> None:
    sandbox = selection.build_tool_sandbox(TOOL)
    assert sandbox.isolation is Isolation.KERNEL_ISOLATED
    assert recorder.made == [(TOOL, "runsc", True)]


def test_without_gvisor_it_falls_back_to_runc_but_never_below_a_container(
    recorder, monkeypatch
) -> None:
    recorder.missing = {"runsc", None}
    with pytest.raises(SandboxUnavailable):
        selection.build_tool_sandbox(TOOL)  # seatbelt・隔離なしへは落ちない
    recorder.missing = {"runsc"}
    assert selection.build_tool_sandbox(TOOL).isolation is Isolation.CONTAINER


def test_the_operators_choice_and_minimum_still_hold(recorder, monkeypatch) -> None:
    monkeypatch.setenv(selection.ENV_BACKEND, "gvisor")
    recorder.missing = {"runsc"}
    with pytest.raises(SandboxUnavailable):
        selection.build_tool_sandbox(TOOL)  # runc へ落ちない
    monkeypatch.setenv(selection.ENV_BACKEND, "auto")
    monkeypatch.setenv(selection.ENV_MINIMUM, "kernel_isolated")
    with pytest.raises(SandboxUnavailable):
        selection.build_tool_sandbox(TOOL)  # runc は最低限に届かない
