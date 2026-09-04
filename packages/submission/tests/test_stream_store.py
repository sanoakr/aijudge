"""ストリーム書き込み・Range 読み出し・サイズ上限の規則を固定する。

動画は数 GB になりうるので、`put`（全体をバイト列）ではなく
`put_stream` / `writer` / `open_read` を通す。原子性は `put` と同じ
（`.partial` に書いてから `os.replace`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_submission import (
    FilesystemArtifactStore,
    SubmissionStoreError,
    iter_file,
    parse_range,
)


def _store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "video")


def test_put_stream_writes_and_reports_size_and_hash(tmp_path: Path) -> None:
    import hashlib

    store = _store(tmp_path)
    parts = [b"abc", b"", b"defgh", b"ij"]
    blob = store.put_stream("ten/sub/art/clip.mp4", parts)
    assert blob.byte_size == 10
    assert blob.sha256 == hashlib.sha256(b"abcdefghij").hexdigest()
    assert store.get("ten/sub/art/clip.mp4") == b"abcdefghij"
    assert store.size("ten/sub/art/clip.mp4") == 10


def test_writer_can_be_aborted_without_leaving_a_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = store.writer("ten/sub/art/clip.mp4")
    handle.write(b"partial")
    assert handle.size == 7
    handle.abort()
    assert not store.exists("ten/sub/art/clip.mp4")
    # `.partial` も残さない。
    assert list((tmp_path / "video" / "ten" / "sub" / "art").glob("*")) == []


def test_put_stream_discards_partial_on_error(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def boom() -> object:
        yield b"good"
        raise RuntimeError("stream died")

    with pytest.raises(RuntimeError):
        store.put_stream("ten/sub/art/clip.mp4", boom())
    assert not store.exists("ten/sub/art/clip.mp4")


def test_open_read_streams_a_range(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_stream("k/v/a/clip.mp4", [bytes(range(256))])
    handle = store.open_read("k/v/a/clip.mp4")
    body = b"".join(iter_file(handle, start=10, length=5))
    assert body == bytes(range(10, 15))


def test_missing_key_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SubmissionStoreError):
        store.open_read("nope/x/y/z.mp4")
    with pytest.raises(SubmissionStoreError):
        store.size("nope/x/y/z.mp4")


def test_key_escaping_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SubmissionStoreError):
        store.writer("../escape.mp4")


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=100-", 1000, (100, 999)),
        ("bytes=-50", 1000, (950, 999)),
        ("bytes=990-5000", 1000, (990, 999)),  # end は size-1 に丸める
        ("bytes=0-0", 1000, (0, 0)),
        (None, 1000, None),
        ("bytes=abc", 1000, None),
        ("bytes=500-100", 1000, None),  # start > end
        ("bytes=0-10,20-30", 1000, None),  # 複数レンジは受けない
        ("bytes=2000-3000", 1000, None),  # 範囲外
    ],
)
def test_parse_range(header: str | None, size: int, expected: tuple[int, int] | None) -> None:
    assert parse_range(header, size) == expected
