"""隔離の下限（`AIJUDGE_SANDBOX_MIN`・#414）が採点の経路で実際に効くこと（#703）。

`test_sandbox_policy.py` は `_at_least` を直接呼ぶだけで、採点が通る
`build_sandbox`（名指し・`auto` の両方）が下限を確かめているかは見ていなかった。
`build_sandbox` から `_at_least` を外しても、`_auto` に下限を渡し忘れても、
スイート全体が緑のままだった（#703 の変異）。

運用機は `AIJUDGE_SANDBOX_MIN=kernel_isolated` に頼っている。runsc が使えなく
なった日に、実提出が黙って runc や seatbelt で動く退行（ADR 0006 が禁じた形）を
ここで止める。

docker も sandbox-exec も呼ばない。バックエンドは作られ方を記録する偽物に
差し替える（`test_tool_sandbox.py` の `_Recorder` と同じ作法）── 見たいのは
選択の規則で、隔離そのものではない。
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from aijudge_sandbox import Isolation, SandboxUnavailable, selection

UNSAFE_CONFIRMATION = "AIJUDGE_SANDBOX_I_KNOW_THIS_IS_UNSAFE"


class _FakeDocker:
    """`DockerSandbox` の代わり。`missing` にある runtime は使えないと申告する。"""

    made: ClassVar[list[str | None]] = []
    missing: ClassVar[set[str | None]] = set()

    def __init__(self, image: str, *, runtime: str | None = None) -> None:
        if runtime in self.missing:
            raise SandboxUnavailable(f"no {runtime or 'runc'}")
        self.made.append(runtime)
        self.name = f"docker:{runtime}" if runtime else "docker"
        self.isolation = Isolation.KERNEL_ISOLATED if runtime == "runsc" else Isolation.CONTAINER


class _FakeSeatbelt:
    """`SeatbeltSandbox` の代わり。CI の Linux でも「使える」ことにする。

    本物は Linux では作れずに例外になるので、本物のままだと下限が効かなくても
    同じ例外で試験が通ってしまう。
    """

    made: ClassVar[int] = 0
    available: ClassVar[bool] = True

    def __init__(self) -> None:
        if not self.available:
            raise SandboxUnavailable("no sandbox-exec")
        type(self).made += 1
        self.name = "seatbelt"
        self.isolation = Isolation.OS_SANDBOX


@pytest.fixture
def backends(monkeypatch: pytest.MonkeyPatch) -> tuple[type[_FakeDocker], type[_FakeSeatbelt]]:
    _FakeDocker.made = []
    _FakeDocker.missing = set()
    _FakeSeatbelt.made = 0
    _FakeSeatbelt.available = True
    monkeypatch.setattr(selection, "DockerSandbox", _FakeDocker)
    monkeypatch.setattr(selection, "SeatbeltSandbox", _FakeSeatbelt)
    for name in (selection.ENV_BACKEND, selection.ENV_MINIMUM, selection.ENV_IMAGE):
        monkeypatch.delenv(name, raising=False)
    return _FakeDocker, _FakeSeatbelt


# --------------------------------------------------------------------------
# 名指しのバックエンド
# --------------------------------------------------------------------------


def test_the_unsafe_backend_is_refused_under_a_minimum(backends, monkeypatch) -> None:
    """二つ目の確認変数を立てても、下限があれば隔離なしでは動かさない。"""
    monkeypatch.setenv(UNSAFE_CONFIRMATION, "yes")
    monkeypatch.setenv(selection.ENV_MINIMUM, "container")
    with pytest.raises(SandboxUnavailable, match="below"):
        selection.build_sandbox("unsafe")


def test_the_unsafe_backend_is_still_built_without_a_minimum(backends, monkeypatch) -> None:
    """上の試験が「unsafe はいつも作れない」ことで通っていないことの確かめ。"""
    monkeypatch.setenv(UNSAFE_CONFIRMATION, "yes")
    assert selection.build_sandbox("unsafe").isolation is Isolation.NONE


def test_a_named_seatbelt_is_refused_under_a_container_minimum(backends, monkeypatch) -> None:
    monkeypatch.setenv(selection.ENV_MINIMUM, "container")
    with pytest.raises(SandboxUnavailable, match="below"):
        selection.build_sandbox("seatbelt")
    assert backends[1].made == 1  # 作れたうえで、下限が断った


def test_runc_named_by_the_environment_is_refused_under_kernel_isolation(
    backends, monkeypatch
) -> None:
    """運用機の設定（`kernel_isolated`）で `AIJUDGE_SANDBOX=docker` を名指しした場合。"""
    monkeypatch.setenv(selection.ENV_BACKEND, "docker")
    monkeypatch.setenv(selection.ENV_MINIMUM, "kernel_isolated")
    with pytest.raises(SandboxUnavailable, match="below"):
        selection.build_sandbox()
    assert backends[0].made == [None]


def test_gvisor_meets_the_kernel_isolation_minimum(backends, monkeypatch) -> None:
    monkeypatch.setenv(selection.ENV_MINIMUM, "kernel_isolated")
    sandbox = selection.build_sandbox("gvisor")
    assert sandbox.isolation is Isolation.KERNEL_ISOLATED


# --------------------------------------------------------------------------
# 自動選択
# --------------------------------------------------------------------------


def test_auto_without_gvisor_does_not_fall_to_runc_under_kernel_isolation(
    backends, monkeypatch
) -> None:
    """**runsc が使えなくなった日の運用機。** runc も seatbelt も使えるが、選ばない。"""
    monkeypatch.setenv(selection.ENV_MINIMUM, "kernel_isolated")
    backends[0].missing = {"runsc"}
    with pytest.raises(SandboxUnavailable) as raised:
        selection.build_sandbox("auto")
    # 候補を試したうえで、下限を理由に断ったこと。
    assert "below" in str(raised.value)
    assert backends[0].made == [None]


def test_auto_without_docker_does_not_fall_to_seatbelt_under_a_container_minimum(
    backends, monkeypatch
) -> None:
    """colima が止まった macOS。seatbelt は fork bomb を封じ込められない（ADR 0006）。"""
    monkeypatch.setenv(selection.ENV_MINIMUM, "container")
    backends[0].missing = {"runsc", None}
    with pytest.raises(SandboxUnavailable, match="below"):
        selection.build_sandbox()
    assert backends[1].made == 1


def test_auto_falls_to_seatbelt_only_when_no_minimum_is_set(backends) -> None:
    """下限が無ければ落ちる。上の 2 件が「そもそも落ちない」ことで通っていないことの確かめ。"""
    backends[0].missing = {"runsc", None}
    assert selection.build_sandbox("auto").isolation is Isolation.OS_SANDBOX


def test_auto_skips_a_weaker_candidate_and_keeps_one_that_meets_the_minimum(
    backends, monkeypatch
) -> None:
    """下限を満たす候補があれば、それを選ぶ（下限は候補を絞るだけで、採点は止めない）。"""
    monkeypatch.setenv(selection.ENV_MINIMUM, "container")
    backends[0].missing = {"runsc"}
    assert selection.build_sandbox("auto").isolation is Isolation.CONTAINER
    assert backends[1].made == 0
