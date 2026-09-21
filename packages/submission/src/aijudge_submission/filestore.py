"""ファイルシステム上の Artifact ストア。

MinIO（S3 互換）に移すまでの実装。プロトコルが同じなので、
差し替えで移行できる。

`storage_key` をそのままパスに使うので、キーに `..` が入ると
ストアの外に書ける。キーは `artifact_storage_key` が作るものだけだが、
「作る側が正しいから安全」は保証ではないので、ここで検証する。

小さい提出（コード・テキスト・PDF・画像）は `put` / `get` で全体を
バイト列として扱う。**動画は数 GB になりうる**ので、`put_stream` /
`open_read` でメモリに載せずに扱う（`ArtifactKind.is_streamed`）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .protocols import SubmissionStoreError

# ストリーム書き込みのチャンク。大きすぎるとメモリ、小さすぎると syscall が増える。
STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StoredBlob:
    """ストリームで書いた結果。呼び出し側が `Artifact` を組むのに使う。"""

    byte_size: int
    # 16 進の SHA-256（`sha256:` 接頭辞は付けない。付けるのは呼び出し側）。
    sha256: str


class BlobWriter:
    """`.partial` に逐次書き、`commit()` で原子的に確定する書き込みハンドル。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._temporary = path.with_name(f".{path.name}.partial")
        self._handle: BinaryIO = self._temporary.open("wb")
        self._digest = hashlib.sha256()
        self._size = 0
        self._done = False

    @property
    def size(self) -> int:
        return self._size

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._handle.write(chunk)
        self._digest.update(chunk)
        self._size += len(chunk)

    def commit(self) -> StoredBlob:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._temporary.replace(self._path)
        self._done = True
        return StoredBlob(byte_size=self._size, sha256=self._digest.hexdigest())

    def abort(self) -> None:
        if self._done:
            return
        self._handle.close()
        self._temporary.unlink(missing_ok=True)


class FilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/"):
            raise SubmissionStoreError(f"invalid storage key: {key!r}")
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root):
            raise SubmissionStoreError(f"storage key escapes the store: {key!r}")
        return target

    def put(self, key: str, payload: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 書き込み途中で落ちた中身を読ませない。提出物の一部だけが
        # 読めてしまうと、採点は成立するのに内容が違う。
        temporary = path.with_name(f".{path.name}.partial")
        temporary.write_bytes(payload)
        temporary.replace(path)

    def writer(self, key: str) -> BlobWriter:
        """1 チャンクずつ書ける書き込みハンドルを返す。

        Web ルートが `async for chunk in request.stream()` の中で
        `writer.write(chunk)` を回すために使う（イベントループを塞がない）。
        `commit()` で `os.replace` により確定、`abort()` で `.partial` を消す。
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return BlobWriter(path)

    def put_stream(self, key: str, chunks: Iterable[bytes]) -> StoredBlob:
        """チャンクの列を書き、サイズと SHA-256 を返す（同期の呼び出し側向け）。

        **メモリに全体を載せない。** `.partial` に逐次書き、ハッシュも
        逐次計算してから `os.replace` で確定する（`put` と同じ原子性）。
        途中で例外が出れば `.partial` を消す。
        """
        handle = self.writer(key)
        try:
            for chunk in chunks:
                handle.write(chunk)
        except BaseException:
            handle.abort()
            raise
        return handle.commit()

    def adopt(self, key: str, source: Path) -> StoredBlob:
        """既にあるファイルを、そのままストアの中身にする（#119）。

        **3 GB を読み直して書き直さない。** 分割アップロードの受け皿は
        同じストアの中（`_incomplete/`）に置くので、確定は `os.replace` で
        済む ── コピーすると、いちばん混んでいる時間に HDD の書き込みが
        倍になる。

        ハッシュはここで計算する。**途中状態は保存できない**（`hashlib` の
        オブジェクトは直列化できない）ので、リクエストをまたいで積み上げる
        道が無い ── 確定の 1 回だけ読み直す。

        別のファイルシステムに跨る場合だけ、明示的にコピーして落とす
        （`os.replace` は同一 FS が要る）。
        """
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        try:
            os.replace(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # 跨いでいる。**コピーしてから消す** ── 消してからコピーすると、
            # 途中で落ちたときに提出が消える。
            shutil.copyfile(source, target)
            source.unlink(missing_ok=True)
        return StoredBlob(byte_size=size, sha256=digest.hexdigest())

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise SubmissionStoreError(f"no artifact stored at {key!r}")
        return path.read_bytes()

    def open_read(self, key: str) -> BinaryIO:
        """読み取り用のファイルハンドルを返す。**呼び出し側が閉じる。**

        Range 応答（`<video>` のシーク）で使う。全体をメモリに読まない。
        """
        path = self._path(key)
        if not path.is_file():
            raise SubmissionStoreError(f"no artifact stored at {key!r}")
        return path.open("rb")

    def size(self, key: str) -> int:
        path = self._path(key)
        if not path.is_file():
            raise SubmissionStoreError(f"no artifact stored at {key!r}")
        return path.stat().st_size

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        """中身を消す。**採点をやり直させるために使わない**（P8）。

        通るのは 2 つだけである ── 受付が途中で失敗したときの後始末と、
        保存期間を過ぎた動画の消去（ADR 0020）。後者は採点済みの提出に効くが、
        消すのは**ファイルだけ**で、`Artifact` の行も採点結果も残る。

        **無いキーは成功とみなす**（`missing_ok`）。消去は「消した印を付ける」
        のと 2 段になっており、途中で落ちるとファイルだけ先に消えた状態が
        残る ── 次の実行がそこから続けられる必要がある。
        """
        self._path(key).unlink(missing_ok=True)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """`Range: bytes=…` を `(start, end)`（両端含む・0 始まり）に直す。

    分からない・不正・満たせない場合は `None`（呼び出し側は 200 で全体を返すか、
    `end < start` を 416 にする）。**単一レンジだけ**扱う ── `<video>` の
    シークはそれで足り、複数レンジは multipart 応答が要るので受けない。
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    spec = header[len("bytes=") :].strip()
    try:
        if spec.startswith("-"):
            # 末尾 N バイト。
            n = int(spec[1:])
            if n <= 0:
                return None
            start = max(0, size - n)
            return (start, size - 1)
        first, _, last = spec.partition("-")
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start < 0 or start > end:
        return None
    return (start, end)


def iter_file(handle: BinaryIO, *, start: int = 0, length: int | None = None) -> Iterator[bytes]:
    """開いたハンドルから `length` バイトを `STREAM_CHUNK_BYTES` ずつ読む。

    Range 応答の本体生成に使う。読み終えたらハンドルを閉じる。
    """
    try:
        handle.seek(start)
        remaining = length
        while True:
            want = STREAM_CHUNK_BYTES if remaining is None else min(STREAM_CHUNK_BYTES, remaining)
            if want <= 0:
                break
            chunk = handle.read(want)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()
