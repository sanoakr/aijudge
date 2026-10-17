"""途中で切れた画像を受付で断る（#633）。

固定したいのは 3 つ。

切れたら断る   PNG の `IEND` が無い・チャンクが途中で尽きる・JPEG の `FFD9` が無い
誤って断らない  後ろにデータが付いた PNG、「動く写真」型の JPEG、確かめない形式
名前で決めない  `.png` という名前の JPEG は JPEG として見る
"""

from __future__ import annotations

import struct
import zlib

import pytest

from aijudge_core import ArtifactKind
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import (
    IncomingFile,
    SubmissionRejected,
    SubmissionService,
    in_memory_backend,
)
from aijudge_submission.image_integrity import INCOMPLETE_IMAGE, image_is_complete


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", pixels)
        + _chunk(b"IEND", b"")
    )


def _jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def test_a_complete_png_passes() -> None:
    assert image_is_complete(_png())


def test_a_png_cut_before_iend_is_incomplete() -> None:
    """2026-10-16 の実物と同じ形: 先頭は正しいが `IEND` に届かない。"""
    whole = _png()
    assert not image_is_complete(whole[: len(whole) - 12])


def test_a_png_cut_inside_a_chunk_is_incomplete() -> None:
    whole = _png()
    assert not image_is_complete(whole[:40])


def test_bytes_after_iend_are_allowed() -> None:
    assert image_is_complete(_png() + b"trailing")


def test_a_jpeg_without_its_end_marker_is_incomplete() -> None:
    assert image_is_complete(_jpeg())
    assert not image_is_complete(_jpeg()[:-2])


def test_a_jpeg_followed_by_a_video_passes() -> None:
    """「動く写真」は JPEG の後ろに動画をつなげて保存する。末尾は `FFD9` ではない。"""
    assert image_is_complete(_jpeg() + b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)


def test_formats_it_does_not_check_pass() -> None:
    assert image_is_complete(b"GIF89a....")
    assert image_is_complete(b"RIFF\x00\x00\x00\x00WEBPVP8 ")


# --------------------------------------------------------------------------
# 受付
# --------------------------------------------------------------------------


@pytest.fixture
def service() -> SubmissionService:
    uow, store = in_memory_backend()
    return SubmissionService(lambda: uow, store)


def _accept(service: SubmissionService, filename: str, payload: bytes):
    return service.accept(
        tenant_id=TenantId("ten_" + "0" * 32),
        task_version_id=TaskVersionId("tsv_" + "1" * 32),
        learner_id=UserId("usr_" + "2" * 32),
        subject_profile="cs_network_python",
        files=[IncomingFile(filename=filename, kind=ArtifactKind.IMAGE, payload=payload)],
    )


def test_a_truncated_image_is_refused_with_a_reason(service: SubmissionService) -> None:
    whole = _png()
    with pytest.raises(SubmissionRejected) as refused:
        _accept(service, "認定証23.png", whole[: len(whole) - 12])
    assert "認定証23.png" in str(refused.value)
    assert INCOMPLETE_IMAGE in str(refused.value)


def test_a_complete_image_is_accepted(service: SubmissionService) -> None:
    assert _accept(service, "認定証23.png", _png()).submission is not None


def test_the_format_comes_from_the_bytes_not_the_name(service: SubmissionService) -> None:
    """`.png` という名前の JPEG は JPEG として見る（名前で PNG と決めると誤って断る）。"""
    assert _accept(service, "photo.png", _jpeg()).submission is not None
