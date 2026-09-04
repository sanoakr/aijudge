"""動画の種別と拡張子の対応を固定する。

動画は取り込み経路が別（`ArtifactKind.is_streamed`）で、既定の受付には
入れない ── 教員が明示的に許した課題でだけ受け付ける。
"""

from __future__ import annotations

from aijudge_core import (
    DEFAULT_UPLOAD_SUFFIXES,
    STREAMED_SUFFIXES,
    ArtifactKind,
    content_type_for,
    kind_for,
)


def test_video_suffixes_map_to_video_kind() -> None:
    for suffix in (".mp4", ".webm", ".mov"):
        assert kind_for(suffix) is ArtifactKind.VIDEO


def test_video_kind_is_streamed_but_not_a_document() -> None:
    assert ArtifactKind.VIDEO.is_streamed is True
    assert ArtifactKind.VIDEO.is_document is False
    assert ArtifactKind.CODE.is_streamed is False


def test_streamed_suffixes_are_exactly_the_video_ones() -> None:
    assert frozenset({".mp4", ".webm", ".mov"}) == STREAMED_SUFFIXES


def test_video_is_not_a_default_accepted_suffix() -> None:
    # 手書き画像・PDF と同じ扱い ── 既定では受けない。
    assert not STREAMED_SUFFIXES & set(DEFAULT_UPLOAD_SUFFIXES)


def test_content_type_for_video() -> None:
    assert content_type_for("demo.mp4") == "video/mp4"
    assert content_type_for("demo.webm") == "video/webm"
    assert content_type_for("demo.mov") == "video/quicktime"
