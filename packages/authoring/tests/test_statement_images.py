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
