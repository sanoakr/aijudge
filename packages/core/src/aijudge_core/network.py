"""「学内からの接続か」を決める（#333）。

試験を学内からだけ受けさせたい、という要求に答えるための判定である。

**ここには機関固有の値を書かない。** このリポジトリは公開物で、学内の
アドレス範囲は機関ごとに違う ── 範囲はテナント管理者が設定し、DB に入る
（`OidcSettings` と同じ扱い）。

## 判定できないことを、通すことと同じにしない

結果は 4 つある。「学内」「学外」のほかに、**範囲が設定されていない**と
**接続元が分からない**を別に持つ。

    NOT_CONFIGURED  範囲が 1 つも無い。制限は効かない ── 効かせると、
                    設定を忘れた瞬間に全員が提出できなくなる。教員の画面に
                    警告を出す側で扱う。
    UNKNOWN         範囲はあるのに接続元が読めない。**断る。** 設定済みの
                    制限が黙って通るほうが悪い（試験のための機能である）。

真偽値 1 つに畳むと、この 2 つが「通す」か「断る」のどちらかに混ざる。
どちらに混ぜても、片方の場面で間違った振る舞いになる。

## 接続元は右端から採る

判定に使うアドレスは `aijudge_telemetry.client_ip` が決める ── `X-Forwarded-For`
の**右端**（逆プロキシが書いた値）である。左端はクライアントが自由に書ける
ので、そこで判定すると `X-Forwarded-For: 10.0.0.1` と名乗るだけで通れる。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from enum import StrEnum

__all__ = ["CampusAccess", "campus_access", "parse_cidrs"]


class CampusAccess(StrEnum):
    """接続元の判定。**通す／断るに畳まない**（モジュール冒頭）。"""

    INSIDE = "inside"
    OUTSIDE = "outside"
    #: 範囲が設定されていない。制限は効かない。
    NOT_CONFIGURED = "not_configured"
    #: 範囲はあるが接続元が読めない。断る側。
    UNKNOWN = "unknown"

    @property
    def allows_submission(self) -> bool:
        """この判定で提出を通してよいか。

        **`NOT_CONFIGURED` は通す。** 設定を忘れた瞬間に全員が締め出される
        作りにはしない ── 気づかせるのは画面の仕事である（`UNKNOWN` は断る）。
        """
        return self in (CampusAccess.INSIDE, CampusAccess.NOT_CONFIGURED)


def parse_cidrs(values: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """設定の文字列を範囲に直す。**読めないものは落とす。**

    落とすのは、1 行の打ち間違いで**残り全部が効かなくなる**のを避けるため
    である。設定画面は保存の前に 1 行ずつ検査して、読めない行をその場で
    突き返す ── ここは保存済みの値を読む側で、既に入っているものに対して
    できるのは「読めるものを使う」だけである。

    ホスト部が立っている表記（`10.20.0.1/19`）も受ける（`strict=False`）──
    人が書き写すときに起きるのはこの形で、意図は範囲の指定で間違いない。
    """
    found = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        try:
            found.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return tuple(found)


def campus_access(client_ip: str | None, cidrs: Iterable[str]) -> CampusAccess:
    """接続元が学内か。`cidrs` はテナント管理者が設定した範囲。

    **範囲が空なら `NOT_CONFIGURED`。** 「空の範囲＝どこも学内ではない」と
    読むと、設定前に制限を入れた問題セットが誰にも解けなくなる。
    """
    networks = parse_cidrs(cidrs)
    if not networks:
        return CampusAccess.NOT_CONFIGURED
    if not client_ip:
        return CampusAccess.UNKNOWN
    try:
        address = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        # 読めないアドレス。**学外ではなく「分からない」** ── 断る点では
        # 同じだが、教員に見せる理由が違う（設定の誤りかもしれない）。
        return CampusAccess.UNKNOWN
    for network in networks:
        # v4 と v6 は混ぜて比べられない。**例外にしない** ── 両方を並べた
        # 設定は正常で、片方に当たらないのは「その行では判定できない」だけ。
        if address.version == network.version and address in network:
            return CampusAccess.INSIDE
    return CampusAccess.OUTSIDE
