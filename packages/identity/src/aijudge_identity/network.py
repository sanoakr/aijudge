"""テナント単位の学内ネットワーク設定（#333）。

**このリポジトリは公開物なので、学内のアドレス範囲はここに書かない。**
OIDC の設定（`oidc.py`）と同じ扱いで、テナント管理者が画面から設定し、
DB に入る値である。

判定そのものは `aijudge_core.network` が持つ ── あちらは範囲と接続元だけを
見る純関数で、保存先も設定画面も知らない。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import TenantId

#: 設定できる範囲の上限。**数の問題ではなく、読める設定にするため。**
#: 無線 LAN の一覧で 15 前後、有線を足しても収まる規模を想定している。
MAX_CIDRS = 64


class CampusNetworkSettings(BaseModel):
    """学内と見なすアドレス範囲。

    **空でも保存できる。** 「まだ調べていない」は正当な状態で、そのときは
    制限が効かない（`CampusAccess.NOT_CONFIGURED`）── 効かせると、設定を
    忘れた瞬間に全員が提出できなくなる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    #: CIDR の並び。順序は画面に出す順で、判定の結果には効かない。
    cidrs: tuple[str, ...] = Field(default=(), max_length=MAX_CIDRS)
