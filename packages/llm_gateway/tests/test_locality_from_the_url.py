"""学内かどうかは申告ではなく宛先で決める（P7・#415）。"""

from __future__ import annotations

import pytest

from aijudge_llm_gateway import OllamaProvider, is_local_url
from aijudge_llm_gateway.locality import ENV_LOCAL_DOMAINS


@pytest.mark.parametrize(
    "url", ["http://localhost:11434", "http://127.0.0.1:11434", "http://10.0.0.5", "http://[::1]:1"]
)
def test_loopback_and_private_addresses_are_local(url: str, monkeypatch) -> None:
    monkeypatch.delenv(ENV_LOCAL_DOMAINS, raising=False)
    assert is_local_url(url)


def test_a_public_host_is_not_local_unless_permitted(monkeypatch) -> None:
    monkeypatch.delenv(ENV_LOCAL_DOMAINS, raising=False)
    assert not is_local_url("http://llm.math.example.ac.jp:11434")
    assert not OllamaProvider("https://api.example.com").capabilities.local
    monkeypatch.setenv(ENV_LOCAL_DOMAINS, "math.example.ac.jp")
    assert is_local_url("http://llm.math.example.ac.jp:11434")
    assert not is_local_url("http://math.example.ac.jp.evil.com")
