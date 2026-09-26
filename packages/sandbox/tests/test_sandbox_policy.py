"""運用者が決める隔離の下限と、使ってよいイメージ（#414・#419）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aijudge_sandbox import SandboxUnavailable
from aijudge_sandbox.backends import DEFAULT_IMAGE
from aijudge_sandbox.selection import (
    ENV_ALLOWED_IMAGES,
    ENV_IMAGE,
    ENV_MINIMUM,
    _at_least,
    _checked_image,
    minimum_isolation,
)
from aijudge_sandbox.types import Isolation


def _backend(isolation: Isolation) -> SimpleNamespace:
    return SimpleNamespace(name=f"fake-{isolation.value}", isolation=isolation)


def test_no_minimum_by_default(monkeypatch) -> None:
    monkeypatch.delenv(ENV_MINIMUM, raising=False)
    assert minimum_isolation() is Isolation.NONE


def test_a_misspelt_minimum_is_not_read_as_none(monkeypatch) -> None:
    """綴り違いを「制限なし」に倒さない。守るつもりの設定が黙って外れる。"""
    monkeypatch.setenv(ENV_MINIMUM, "containr")
    with pytest.raises(SandboxUnavailable, match="not an isolation level"):
        minimum_isolation()


def test_a_backend_below_the_minimum_is_refused() -> None:
    """seatbelt は fork bomb を封じ込められない（ADR 0006）。下限があれば断る。"""
    with pytest.raises(SandboxUnavailable, match="below"):
        _at_least(_backend(Isolation.OS_SANDBOX), Isolation.CONTAINER)


@pytest.mark.parametrize("isolation", [Isolation.CONTAINER, Isolation.KERNEL_ISOLATED])
def test_a_backend_at_or_above_the_minimum_is_kept(isolation: Isolation) -> None:
    backend = _backend(isolation)
    assert _at_least(backend, Isolation.CONTAINER) is backend


def test_the_operators_images_are_always_allowed(monkeypatch) -> None:
    monkeypatch.delenv(ENV_ALLOWED_IMAGES, raising=False)
    monkeypatch.setenv(ENV_IMAGE, "registry.local/aijudge:1")
    assert _checked_image(DEFAULT_IMAGE) == DEFAULT_IMAGE
    assert _checked_image("registry.local/aijudge:1") == "registry.local/aijudge:1"


def test_an_unlisted_image_from_a_course_is_refused(monkeypatch) -> None:
    """コースの採点設定から任意のレジストリを pull させない（#419）。"""
    monkeypatch.delenv(ENV_ALLOWED_IMAGES, raising=False)
    monkeypatch.delenv(ENV_IMAGE, raising=False)
    with pytest.raises(SandboxUnavailable, match=ENV_ALLOWED_IMAGES):
        _checked_image("evil.example/pwn:latest")


def test_a_listed_image_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ALLOWED_IMAGES, "python:3.13-bookworm, node:22")
    assert _checked_image("python:3.13-bookworm") == "python:3.13-bookworm"


@pytest.mark.parametrize("image", ["--privileged", "-v/:/host", "gcc 14", ""])
def test_a_value_that_is_not_an_image_reference_is_refused(monkeypatch, image: str) -> None:
    """docker の引数に入るので、オプションとして読まれうる値を通さない。"""
    monkeypatch.setenv(ENV_ALLOWED_IMAGES, image)
    with pytest.raises(SandboxUnavailable):
        _checked_image(image)
