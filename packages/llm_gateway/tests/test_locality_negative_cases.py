"""学内判定が「学内ではない」と言うべき URL を固定する（#415・#690）。

`test_locality_from_the_url.py` の否定例は、どれもドメイン照合の側しか
通らない。ここでは IP を直接書いた場合・`localhost` に似せた名前・
userinfo で宛先を偽る書き方を足す。どれか 1 つでも学内と判定されると、
`AIJUDGE_LLM_BASE_URL` の書き方ひとつで学習者のデータが学外へ出る。
"""

from __future__ import annotations

import pytest

from aijudge_llm_gateway import OllamaProvider, is_local_url
from aijudge_llm_gateway.locality import ENV_LOCAL_DOMAINS


@pytest.fixture(autouse=True)
def _no_permitted_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_LOCAL_DOMAINS, raising=False)


@pytest.mark.parametrize(
    "url",
    [
        # 公開アドレスを直接書いても学内にはならない。
        "http://8.8.8.8:11434",
        "http://[2001:4860:4860::8888]:11434",
        # `localhost` で始まるだけの外部の名前。
        "http://localhost.evil.com:11434",
        "http://localhost-llm.example.com",
        # userinfo に `localhost` を書いても、宛先は `@` の後ろ。
        "http://localhost@evil.com:11434",
        "http://localhost:11434@evil.com",
        # スキームが無いと宛先を読めない。読めないものは学内としない。
        "localhost:11434",
        "",
    ],
)
def test_these_destinations_are_not_local(url: str) -> None:
    assert not is_local_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:11434",
        "http://172.16.0.1",
        "http://169.254.10.20:11434",
        "http://[fe80::1]:11434",
        "http://llm.localhost:11434",
    ],
)
def test_private_and_link_local_addresses_are_local(url: str) -> None:
    """肯定側の境界も置く。否定例だけだと「全部 False」の実装が通る。"""
    assert is_local_url(url)


def test_the_provider_takes_its_locality_from_the_address() -> None:
    """申告ではなく宛先で決まる。公開アドレスを書いたプロバイダは学外である。"""
    assert not OllamaProvider("http://8.8.8.8:11434").capabilities.local
    assert not OllamaProvider("http://localhost@evil.com").capabilities.local
    assert OllamaProvider("http://192.168.1.20:11434").capabilities.local
