"""提出物を返すときの `Content-Disposition` を固定する。

HTTP ヘッダは latin-1 でしか送れない。ファイル名をそのまま埋めると、
macOS の日本語環境のスクリーンショット（「スクリーンショット 2026-09-17
10.00.00.png」）で応答が組み立てられず 500 になり、画像提出の課題で
採点画面に出ない提出が出た。**空白は問題ではなく、非 ASCII が問題。**
"""

from __future__ import annotations

from aijudge_core import content_disposition

FALLBACK = "art_" + "0" * 32


def test_an_ascii_name_is_quoted_as_is() -> None:
    assert content_disposition("inline", "shot.png", FALLBACK) == 'inline; filename="shot.png"'


def test_spaces_alone_do_not_need_the_extended_form() -> None:
    header = content_disposition("attachment", "my shot 1.png", FALLBACK)
    assert header == 'attachment; filename="my shot 1.png"'


def test_a_non_ascii_name_is_sent_in_the_extended_form_with_an_ascii_fallback() -> None:
    header = content_disposition("inline", "スクリーンショット 2026-09-17 10.00.00.png", FALLBACK)
    # ヘッダとして送れる（latin-1 に収まる）。
    header.encode("latin-1")
    assert header.startswith("inline; filename=\" 2026-09-17 10.00.00.png\"; filename*=UTF-8''")
    assert header.endswith(
        "%E3%82%B9%E3%82%AF%E3%83%AA%E3%83%BC%E3%83%B3%E3%82%B7%E3%83%A7%E3%83%83%E3%83%88"
        "%202026-09-17%2010.00.00.png"
    )


def test_a_name_with_nothing_ascii_but_the_suffix_falls_back_to_the_id() -> None:
    header = content_disposition("inline", "答案.png", FALLBACK)
    assert header.startswith(f"inline; filename=\"{FALLBACK}.png\"; filename*=UTF-8''")


def test_quotes_and_newlines_cannot_break_the_header() -> None:
    header = content_disposition("attachment", 'a"b\\c\r\nX: y.png', FALLBACK)
    assert "\n" not in header and "\r" not in header
    assert header.startswith("attachment; filename=\"abcX: y.png\"; filename*=UTF-8''")


def test_a_missing_name_uses_the_fallback() -> None:
    assert content_disposition("inline", None, FALLBACK) == f'inline; filename="{FALLBACK}"'
