"""課題文に貼る画像の鍵と名前（#64）。

**鍵は URL から機械的に導ける。** 導けないと URL と鍵の対応を別に持つことに
なり、その表が失われた時点で課題文の画像が全部行方不明になる。
"""

from __future__ import annotations

import pytest

from aijudge_authoring import images
from aijudge_authoring.statement import render_statement


def test_the_name_comes_from_the_content() -> None:
    """同じ画像を貼り直しても名前が変わらない ── 課題文の URL も変わらない。"""
    first = images.new_name(b"same bytes", "shot.png")
    second = images.new_name(b"same bytes", "shot.png")
    assert first == second
    assert images.new_name(b"other bytes", "shot.png") != first


def test_the_suffix_survives_a_capitalised_filename() -> None:
    assert images.new_name(b"x", "SHOT.PNG").endswith(".png")


def test_an_unsupported_format_is_refused() -> None:
    """**課題文に貼れる形式だけ。** 提出できる形式とは別に持つ（PDF は貼れない）。"""
    with pytest.raises(images.ImageError):
        images.new_name(b"x", "report.pdf")


def test_an_oversized_image_is_refused() -> None:
    with pytest.raises(images.ImageError):
        images.new_name(b"x" * (images.MAX_BYTES + 1), "big.png")


def test_the_key_carries_the_course() -> None:
    """URL からコースが分かる。**誰に見せてよいかを DB を引かずに決められる。**"""
    name = images.new_name(b"x", "a.png")
    assert images.storage_key("crs_1", name) == f"statement-images/crs_1/{name}"


def test_a_name_that_looks_like_a_path_is_refused() -> None:
    """**ストアの外へ出させない。** 名前は中身から導いた 32 桁だけを受ける。"""
    for bad in ("../../etc/passwd.png", "a/b.png", "hello.png"):
        with pytest.raises(images.ImageError):
            images.storage_key("crs_1", bad)


def test_the_snippet_renders_as_an_image() -> None:
    """**描画はもともと通る。** 足りていなかったのは置き場所だけ。"""
    name = images.new_name(b"x", "a.png")
    html = render_statement(images.markdown_for("crs_1", name, "端末の画面"))
    assert f'<img src="/images/crs_1/{name}" alt="端末の画面"' in html


# --------------------------------------------------------------------------
# 貼ったときの表示幅（#64）
# --------------------------------------------------------------------------


def _png(width: int, height: int = 10) -> bytes:
    """幅だけが本物の PNG の先頭。**寸法は符号の先頭にある。**"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def _jpeg(width: int, height: int = 10) -> bytes:
    """SOF0 を 1 つ持つ JPEG の先頭（前に節を 1 つ挟んでおく）。"""
    app0 = b"\xff\xe0" + (6).to_bytes(2, "big") + b"JFIF"
    sof = (
        b"\xff\xc0"
        + (11).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof


def test_the_width_comes_from_the_image_itself() -> None:
    """**符号の先頭だけを読む。** 幅を知るために画像を展開する依存を増やさない。"""
    assert images.intrinsic_width(_png(1200)) == 1200
    assert images.intrinsic_width(_jpeg(3776)) == 3776
    assert images.intrinsic_width(b"GIF89a" + (640).to_bytes(2, "little") + b"\x00\x00") == 640
    # 読めない形式（SVG など）は幅を言わない。**分からない数を書かない。**
    assert images.intrinsic_width(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None


def test_a_large_image_is_pasted_at_the_reading_width() -> None:
    """**縮めずに貼ると写真 1 枚で画面が埋まる。** 課題文の続きが画面外へ出る。"""
    assert images.display_width(_png(4000)) == images.DISPLAY_WIDTH


def test_a_small_image_keeps_its_own_size() -> None:
    """元より大きく引き伸ばさない ── 粗くなるだけで、理由は画面から分からない。"""
    assert images.display_width(_png(320)) is None


def test_the_snippet_carries_the_width_but_not_the_height() -> None:
    """**幅だけを書く。** 両方書けると、幅を直したときに絵が歪む。"""
    line = images.markdown_for("crs_1", images.new_name(b"x", "a.png"), "図", width=480)
    assert line.endswith("{width=480}")
    html = render_statement(line)
    assert 'width="480"' in html
    assert "height=" not in html


def test_the_statement_takes_no_attribute_other_than_the_width() -> None:
    """**`html=False` を緩めない。** Phase 4 で課題文はモデルの出力になる。

    通さない属性が 1 つでも混じっていれば、その `{...}` は属性として読まれず
    そのまま文字として出る（画像は素のまま描かれる）。
    """
    html = render_statement("![x](/images/crs_1/a.png){width=480 onerror=alert(1)}")
    assert "onerror" not in html.replace("onerror=alert(1)}", "")
    assert 'width="480"' not in html
