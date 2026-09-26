"""接続先が「学内」かを URL から決める（P7・#415）。

以前は `OllamaProvider(local=True)` が既定で、環境変数の URL をそのまま
渡していた。`PolicyViolation` はこの**自己申告**しか見ないので、
`AIJUDGE_LLM_BASE_URL` に学外のホスト（クラウドの ollama 互換 API など）を
書けば、学習者のデータ（`DataClass.PERSONAL`）がそのまま送られた。守りは
コメントと運用だけだった。

ここでは申告ではなく**宛先で**決める。学内と認めるのは次のものだけ。

- `localhost` とループバック・プライベート・リンクローカルのアドレス（IP を直接書いた場合）
- `AIJUDGE_LLM_LOCAL_DOMAINS`（カンマ区切り）に挙げたドメインとその配下

**名前解決はしない。** 解決結果は後から変わりうる（DNS の書き換え）し、
大学の従系は大学のグローバルアドレスにあるので、アドレスの種類では
学内かを言えない。学内と認める名前は運用者が明示する。機関固有の値
（ドメイン名）はこのリポジトリの既定に入れない ── 公開物だからである。
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

ENV_LOCAL_DOMAINS = "AIJUDGE_LLM_LOCAL_DOMAINS"


def _permitted_domains() -> tuple[str, ...]:
    raw = os.environ.get(ENV_LOCAL_DOMAINS, "")
    return tuple(item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip())


def is_local_url(url: str) -> bool:
    """この URL の宛先に学習者のデータを送ってよいか。"""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return any(host == domain or host.endswith(f".{domain}") for domain in _permitted_domains())
    return address.is_loopback or address.is_private or address.is_link_local


__all__ = ["ENV_LOCAL_DOMAINS", "is_local_url"]
