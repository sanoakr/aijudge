"""学内からの接続かを決める規則（#333）。

固定したいのは 4 つ。

畳まない       「未設定」と「判定できない」を通す／断るに混ぜない。
未設定は通す   設定を忘れた瞬間に全員が締め出される作りにしない。
不明は断る     設定済みの制限が黙って抜けるほうが悪い（試験のための機能）。
偽れない       判定に使うのは逆プロキシが書いた値（呼び出し側の責務も明記）。
"""

from __future__ import annotations

from aijudge_core import CampusAccess, campus_access, parse_cidrs

CAMPUS = ("133.83.80.0/24", "133.83.82.0/25")


def test_an_address_inside_the_range_is_inside() -> None:
    assert campus_access("133.83.80.110", CAMPUS) is CampusAccess.INSIDE


def test_an_address_outside_every_range_is_outside() -> None:
    assert campus_access("82.26.195.14", CAMPUS) is CampusAccess.OUTSIDE


def test_no_ranges_means_not_configured_and_still_allows() -> None:
    """**空の範囲を「どこも学内ではない」と読まない。**

    読むと、範囲を設定する前に制限を入れた問題セットが誰にも解けなくなる
    ── 試験当日に全員が止まる形である。
    """
    access = campus_access("133.83.80.110", ())

    assert access is CampusAccess.NOT_CONFIGURED
    assert access.allows_submission is True


def test_an_unreadable_source_is_unknown_and_refused() -> None:
    """**設定済みの制限が黙って抜けるほうが悪い。**

    「学外」とは別にしておく ── 学習者にとって意味が違う（場所を移せば
    直るのか、移しても直らないのか）。
    """
    for value in (None, "", "   ", "nonsense", "1.2.3"):
        access = campus_access(value, CAMPUS)
        assert access is CampusAccess.UNKNOWN, value
        assert access.allows_submission is False


def test_the_four_states_do_not_collapse_into_a_boolean() -> None:
    """通す／断るの 2 値に畳むと、片方の場面で必ず間違う。"""
    allowed = {a for a in CampusAccess if a.allows_submission}

    assert allowed == {CampusAccess.INSIDE, CampusAccess.NOT_CONFIGURED}


def test_a_broken_line_does_not_disable_the_rest() -> None:
    """**1 行の打ち間違いで残り全部が効かなくなる**のを避ける。

    読めない行をその場で突き返すのは設定画面の仕事で、ここは保存済みの値を
    読む側である。
    """
    assert campus_access("133.83.80.5", ("これは範囲ではない", "133.83.80.0/24")) is (
        CampusAccess.INSIDE
    )


def test_a_host_address_with_bits_set_is_read_as_the_range() -> None:
    """人が書き写すときに起きるのはこの形で、意図は範囲の指定で間違いない。"""
    assert campus_access("10.20.5.7", ("10.20.0.1/19",)) is CampusAccess.INSIDE


def test_v4_and_v6_are_not_compared_against_each_other() -> None:
    """両方を並べた設定は正常。片方に当たらないのは異常ではない。"""
    mixed = ("133.83.80.0/24", "2001:db8::/32")

    assert campus_access("2001:db8::1", mixed) is CampusAccess.INSIDE
    assert campus_access("133.83.80.1", mixed) is CampusAccess.INSIDE
    assert campus_access("10.0.0.1", mixed) is CampusAccess.OUTSIDE


def test_parse_drops_only_what_it_cannot_read() -> None:
    assert len(parse_cidrs(("133.83.80.0/24", "", "  ", "だめ", "10.0.0.0/8"))) == 2
