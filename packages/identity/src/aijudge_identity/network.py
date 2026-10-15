"""テナント単位の学内ネットワーク設定（#333）。

**このリポジトリは公開物なので、学内のアドレス範囲はここに書かない。**
OIDC の設定（`oidc.py`）と同じ扱いで、テナント管理者が画面から設定し、
DB に入る値である。

判定そのものは `aijudge_core.network` が持つ ── あちらは範囲と接続元だけを
見る純関数で、保存先も設定画面も知らない。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aijudge_core.ids import TenantId

#: 設定できる範囲の上限。**数の問題ではなく、読める設定にするため。**
#: 無線 LAN の一覧で 15 前後、有線を足しても収まる規模を想定している。
MAX_CIDRS = 64


#: 注釈の長さの上限。教室名と回線の種類が書ければ足りる。
MAX_NOTE_LENGTH = 120

#: まとまり（見出し）の名前の長さの上限。キャンパス名や「情報実習室」が書ければ足りる。
MAX_GROUP_LENGTH = 40

#: 登録欄で見出しを表す行頭（#621）。`## 瀬田` の下の範囲が「瀬田」にまとまる。
GROUP_HEADING = "##"


class CampusRange(BaseModel):
    """学内の 1 範囲と、それが**どの教室・どの回線か**の注釈。

    範囲は人が調べて書き写す値で、CIDR だけでは教員にどれを選べばよいか
    読めない。注釈（「3 号館 301 教室・有線」など）を持たせ、教員が問題セットで
    受け付ける場所を選ぶときに読めるようにする（2026-10-02）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 正規化した CIDR（`ipaddress.ip_network(..., strict=False)` の文字列）。
    #: **問題セット側の選択はこの文字列で範囲を指す** ── 別の ID を振ると、
    #: 範囲を書き直すたびに選択との対応を持ち替える場所が増える。
    cidr: str
    note: str = Field(default="", max_length=MAX_NOTE_LENGTH)
    #: どのまとまり（キャンパス・教室など）に入るか。教員が選ぶ画面で見出しになる
    #: （#621）。**判定にも選択の照合にも使わない** ── 照合は CIDR だけなので、
    #: まとめ方を変えても、どの問題セットの受付も変わらない。空はまとまり無し。
    group: str = Field(default="", max_length=MAX_GROUP_LENGTH)


class CampusNetworkSettings(BaseModel):
    """学内と見なすアドレス範囲。

    **空でも保存できる。** 「まだ調べていない」は正当な状態で、そのときは
    制限が効かない（`CampusAccess.NOT_CONFIGURED`）── 効かせると、設定を
    忘れた瞬間に全員が提出できなくなる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    #: 範囲の並び。順序は画面に出す順で、判定の結果には効かない。
    ranges: tuple[CampusRange, ...] = Field(default=(), max_length=MAX_CIDRS)

    @model_validator(mode="before")
    @classmethod
    def _from_plain_cidrs(cls, data: object) -> object:
        """`cidrs=(...)` で作る旧い呼び方を受ける（注釈は空の範囲として）。"""
        if isinstance(data, dict) and "cidrs" in data and "ranges" not in data:
            plain = dict(data)
            plain["ranges"] = tuple(CampusRange(cidr=c) for c in plain.pop("cidrs"))
            return plain
        return data

    @property
    def cidrs(self) -> tuple[str, ...]:
        """範囲の文字列だけ。判定（`campus_access`）に渡す形。"""
        return tuple(entry.cidr for entry in self.ranges)

    def selected(self, chosen: tuple[str, ...]) -> tuple[CampusRange, ...]:
        """問題セットが選んだ範囲（`Task.campus_ranges`）に当たるもの。

        **選択が空なら全範囲**（移行前に学内限定にした問題セットを、これまで
        通り効かせる）。選んだものがどれも残っていないときは空を返す ──
        呼び出し側が「判定できない」として断る（全範囲に化けさせない）。
        """
        if not chosen:
            return self.ranges
        wanted = set(chosen)
        return tuple(entry for entry in self.ranges if entry.cidr in wanted)

    def grouped(self) -> tuple[tuple[str, tuple[CampusRange, ...]], ...]:
        """まとまりごとの範囲（`group_ranges`）。"""
        return group_ranges(self.ranges)


def group_ranges(
    ranges: tuple[CampusRange, ...],
) -> tuple[tuple[str, tuple[CampusRange, ...]], ...]:
    """まとまりごとの範囲。**見出しが最初に出た順**で、中の並びも登録順（#621）。

    まとまりの無い範囲は名前が空のまとまりに入る。登録欄で見出しより前に
    書いた範囲と、見出しを使う前に登録した範囲がここに来る。
    """
    order: list[str] = []
    members: dict[str, list[CampusRange]] = {}
    for entry in ranges:
        if entry.group not in members:
            order.append(entry.group)
            members[entry.group] = []
        members[entry.group].append(entry)
    return tuple((name, tuple(members[name])) for name in order)
