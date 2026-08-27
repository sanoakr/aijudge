"""どのバックエンドを使うかの決定。

規則は 1 つ。**隔離なしには自動で落ちない。**
使える隔離が無ければ例外にして採点を失敗させる。隔離なしで採点が
通ってしまうより、採点が止まる方がましだから。
"""

from __future__ import annotations

import os
import platform

from .backends import DEFAULT_IMAGE, DockerSandbox, SeatbeltSandbox, UnsafeLocalSandbox
from .base import Sandbox
from .types import SandboxUnavailable

ENV_BACKEND = "AIJUDGE_SANDBOX"
ENV_IMAGE = "AIJUDGE_SANDBOX_IMAGE"

BACKENDS = ("auto", "seatbelt", "docker", "gvisor", "unsafe")


def build_sandbox(name: str | None = None, *, image: str | None = None) -> Sandbox:
    """名前からバックエンドを作る。既定は環境変数、無ければ自動選択。"""
    choice = (name or os.environ.get(ENV_BACKEND) or "auto").strip().lower()
    picked = image or os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE

    if choice == "auto":
        return _auto(picked)
    if choice == "seatbelt":
        return SeatbeltSandbox()
    if choice == "docker":
        return DockerSandbox(picked)
    if choice == "gvisor":
        return DockerSandbox(picked, runtime="runsc")
    if choice == "unsafe":
        # 環境変数だけで隔離を外せてしまうと、設定ミスが静かに事故になる。
        # 二つ目の変数を要求して、意図的な操作でしか外れないようにする。
        if os.environ.get("AIJUDGE_SANDBOX_I_KNOW_THIS_IS_UNSAFE") != "yes":
            raise SandboxUnavailable(
                "the unsafe backend also needs "
                "AIJUDGE_SANDBOX_I_KNOW_THIS_IS_UNSAFE=yes; "
                "never set it where real submissions are graded"
            )
        return UnsafeLocalSandbox(allow_unsafe=True)

    raise SandboxUnavailable(f"unknown sandbox backend {choice!r}; pick one of {BACKENDS}")


def _auto(image: str) -> Sandbox:
    """使える中で最も強い隔離を選ぶ。無ければ例外。"""
    attempts: list[str] = []

    for factory, label in (
        (lambda: DockerSandbox(image, runtime="runsc"), "gvisor"),
        (lambda: DockerSandbox(image), "docker"),
        (SeatbeltSandbox, "seatbelt"),
    ):
        try:
            return factory()
        except SandboxUnavailable as exc:
            attempts.append(f"{label}: {exc}")

    raise SandboxUnavailable(
        "no isolation backend is available on this host, so submissions cannot be "
        "executed safely. Install Docker (or colima) and retry. Tried — "
        + "; ".join(attempts)
        + f". Platform: {platform.system()} {platform.machine()}."
    )


def default_sandbox() -> Sandbox:
    return build_sandbox()
