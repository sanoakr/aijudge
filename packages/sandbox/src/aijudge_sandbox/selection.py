"""どのバックエンドを使うかの決定。

規則は 1 つ。**隔離なしには自動で落ちない。**
使える隔離が無ければ例外にして採点を失敗させる。隔離なしで採点が
通ってしまうより、採点が止まる方がましだから。
"""

from __future__ import annotations

import os
import platform
import re

from .backends import DEFAULT_IMAGE, DockerSandbox, SeatbeltSandbox, UnsafeLocalSandbox
from .base import Sandbox
from .types import Isolation, SandboxUnavailable

ENV_BACKEND = "AIJUDGE_SANDBOX"
ENV_IMAGE = "AIJUDGE_SANDBOX_IMAGE"
# これより弱い隔離では動かさない（#414）。未設定なら制限しない（開発機の
# seatbelt を止めないため）。**本番では `container` 以上を入れる。**
ENV_MINIMUM = "AIJUDGE_SANDBOX_MIN"
# 既定以外に使ってよいイメージ（カンマ区切り、#419）。コースの採点設定
# （`evaluator_options.*.image`）から名指しできるのはここにあるものだけ。
ENV_ALLOWED_IMAGES = "AIJUDGE_SANDBOX_IMAGES"

#: 隔離の強さの順。`ENV_MINIMUM` の比較に使う。
ISOLATION_ORDER = (
    Isolation.NONE,
    Isolation.OS_SANDBOX,
    Isolation.CONTAINER,
    Isolation.KERNEL_ISOLATED,
)

# docker のイメージ参照（`registry/name:tag@digest` の範囲）。**`-` で始まる値を
# 通さない** ── docker の引数に入るので、オプションとして読まれうる。
_IMAGE_REFERENCE = re.compile(r"[a-z0-9][a-z0-9._/:@-]{0,254}", re.IGNORECASE)

BACKENDS = ("auto", "seatbelt", "docker", "gvisor", "unsafe")


def build_sandbox(name: str | None = None, *, image: str | None = None) -> Sandbox:
    """名前からバックエンドを作る。既定は環境変数、無ければ自動選択。"""
    choice = (name or os.environ.get(ENV_BACKEND) or "auto").strip().lower()
    picked = _checked_image(image) if image else os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE
    minimum = minimum_isolation()

    if choice == "auto":
        return _auto(picked, minimum)
    return _at_least(_named(choice, picked), minimum)


def minimum_isolation() -> Isolation:
    """運用者が求める最低限の隔離（`AIJUDGE_SANDBOX_MIN`）。"""
    raw = os.environ.get(ENV_MINIMUM, "").strip().lower()
    if not raw:
        return Isolation.NONE
    try:
        return Isolation(raw)
    except ValueError:
        # 綴り違いを「制限なし」に倒さない。守るつもりの設定が黙って外れる。
        raise SandboxUnavailable(
            f"{ENV_MINIMUM}={raw!r} is not an isolation level; "
            f"pick one of {[level.value for level in ISOLATION_ORDER]}"
        ) from None


def _at_least(sandbox: Sandbox, minimum: Isolation) -> Sandbox:
    """最低限に届かないバックエンドを断る（#414）。

    自動選択は使える中で最も強いものを選ぶが、強いものが使えなければ黙って
    弱いものに落ちる。macOS で colima が止まっているだけで、実提出が
    fork bomb を封じ込められない seatbelt で動く（ADR 0006 が禁じた形）。
    """
    if ISOLATION_ORDER.index(sandbox.isolation) < ISOLATION_ORDER.index(minimum):
        raise SandboxUnavailable(
            f"the {sandbox.name} backend isolates at {sandbox.isolation.value}, below "
            f"{ENV_MINIMUM}={minimum.value}; refusing to run submissions with it"
        )
    return sandbox


def _checked_image(image: str) -> str:
    """採点設定から名指しされたイメージを確かめる（#419）。

    コースの採点設定は教員が `/manage` から書ける。確かめずに docker に渡すと、
    任意のレジストリから pull させられる（提出物がそのイメージの中で動く）。
    既定と `AIJUDGE_SANDBOX_IMAGE` は運用者が決めたものなので常に通す。
    """
    image = image.strip()
    if not _IMAGE_REFERENCE.fullmatch(image):
        raise SandboxUnavailable(f"{image!r} is not a container image reference")
    allowed = {DEFAULT_IMAGE, os.environ.get(ENV_IMAGE, DEFAULT_IMAGE)} | {
        item.strip() for item in os.environ.get(ENV_ALLOWED_IMAGES, "").split(",") if item.strip()
    }
    if image not in allowed:
        raise SandboxUnavailable(
            f"image {image!r} is not in {ENV_ALLOWED_IMAGES}; ask the operator to allow it"
        )
    return image


def _named(choice: str, picked: str) -> Sandbox:
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


def _auto(image: str, minimum: Isolation = Isolation.NONE) -> Sandbox:
    """使える中で最も強い隔離を選ぶ。無ければ例外。

    最低限（`AIJUDGE_SANDBOX_MIN`）より弱いものは候補にしない。
    """
    attempts: list[str] = []

    for factory, label in (
        (lambda: DockerSandbox(image, runtime="runsc"), "gvisor"),
        (lambda: DockerSandbox(image), "docker"),
        (SeatbeltSandbox, "seatbelt"),
    ):
        try:
            return _at_least(factory(), minimum)
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
