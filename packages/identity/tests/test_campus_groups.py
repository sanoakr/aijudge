"""学内ネットワークのまとまり（#621）。"""

from __future__ import annotations

from aijudge_identity.network import CampusRange, group_ranges


def test_groups_keep_the_order_their_heading_first_appeared() -> None:
    ranges = (
        CampusRange(cidr="10.0.0.0/24", group="瀬田"),
        CampusRange(cidr="10.0.1.0/24", group="深草"),
        CampusRange(cidr="10.0.2.0/24", group="瀬田"),
        CampusRange(cidr="10.0.3.0/24"),
    )

    grouped = group_ranges(ranges)

    assert [name for name, _ in grouped] == ["瀬田", "深草", ""]
    assert [r.cidr for r in grouped[0][1]] == ["10.0.0.0/24", "10.0.2.0/24"]


def test_a_range_saved_before_groups_existed_has_no_group() -> None:
    """`group` を持たない旧い保存も読める（空のまとまり）。"""
    assert CampusRange(cidr="10.0.0.0/24", note="旧い登録").group == ""
