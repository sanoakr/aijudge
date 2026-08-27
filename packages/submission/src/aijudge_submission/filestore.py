"""ファイルシステム上の Artifact ストア。

MinIO（S3 互換）に移すまでの実装。プロトコルが同じなので、
差し替えで移行できる。

`storage_key` をそのままパスに使うので、キーに `..` が入ると
ストアの外に書ける。キーは `artifact_storage_key` が作るものだけだが、
「作る側が正しいから安全」は保証ではないので、ここで検証する。
"""

from __future__ import annotations

from pathlib import Path

from .protocols import SubmissionStoreError


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

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise SubmissionStoreError(f"no artifact stored at {key!r}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
