"""中断したところから続けられる動画アップロード（#119）。

学内 Wi-Fi 経由で数 GB を送る。**90% で切れたら全部やり直し**というのが
1 回の PUT の性質で、試験のように時間窓が決まっている場面ではそれが
「間に合わなかった」に直結する。

## 仕組み

セッションを 1 つ作り、クライアントは固定長で分けて送る。切れたら
**いまサーバに何バイト届いているか**を訊き、その続きから送る。

    POST   /tasks/{tv}/uploads          セッションを作る（上限と分割長を返す）
    GET    /uploads/{id}                いまのオフセットを訊く
    PATCH  /uploads/{id}?offset=N       続きを書く
    POST   /uploads/{id}/finish         確定する（提出になる）

tus は使わない。**この配備には JavaScript のビルド経路が無く**、クライアント
は素の XHR で書かれている ── 仕様とライブラリを 1 本抱える代償のほうが、
「オフセットを訊いて続きから送る」を自分で書く手間より大きい。

## 置き場所

受け皿は**動画ストアと同じ根の下**（`_incomplete/`）に置く。確定が
`os.replace` で済む ── 別の場所に置くと、いちばん混んでいる時間に 3 GB の
コピーが増える（`os.replace` は同一ファイルシステムを要求する）。

## 持ち主を書いておく

`.json` に提出者・課題・上限を残す。**続きを書けるのは作った本人だけ**で、
これを持たないと、セッション id を知っている誰でも他人の提出に追記できる。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .protocols import SubmissionStoreError

#: 受け皿の置き場所（動画ストアの根からの相対）。
INCOMPLETE_DIR = "_incomplete"

#: 触られないまま残ったセッションを消すまで。**締切をまたいで残さない。**
#: 提出の途中で諦めた学習者の数 GB が、ディスクに積み上がり続ける。
DEFAULT_TTL = timedelta(hours=24)


class UploadSessionError(SubmissionStoreError):
    """セッションが無い・持ち主が違う・期限切れ。"""


class OffsetMismatch(SubmissionStoreError):
    """クライアントが思っている位置と、サーバにあるバイト数が違う。

    **サーバの値を返す。** クライアントはそこから送り直せばよく、
    やり直す必要は無い（それがこの仕組みの目的である）。
    """

    def __init__(self, offset: int) -> None:
        super().__init__(f"offset mismatch; server has {offset} bytes")
        self.offset = offset


class TooLarge(SubmissionStoreError):
    """分割をまたいで上限を超えた。"""


@dataclass(frozen=True)
class UploadSession:
    """1 本のアップロード。`offset` は**ファイルの実サイズ**から引く。

    別に数えて持つと、書き込みが落ちたときに「記録上は届いている」が
    起きる ── クライアントはその続きから送り、**穴の空いた動画**ができる。
    """

    id: str
    tenant_id: str
    learner_id: str
    task_version_id: str
    filename: str
    max_bytes: int
    created_at: datetime
    offset: int

    def belongs_to(self, *, tenant_id: str, learner_id: str) -> bool:
        return self.tenant_id == tenant_id and self.learner_id == learner_id


class FilesystemUploadSessions:
    """受け皿をファイルシステムに持つ実装。

    根は**動画ストアと同じ**にすること（確定の `os.replace` が同一 FS を
    要求する）。`<id>.part` が中身で、`<id>.json` が持ち主と上限である。
    """

    def __init__(self, root: Path, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self.root = root.resolve() / INCOMPLETE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl

    # -- 置き場所 ----------------------------------------------------------

    def _paths(self, upload_id: str) -> tuple[Path, Path]:
        # id は `new_id` が作るものだけだが、**作る側が正しいから安全**は
        # 保証ではない（`FilesystemArtifactStore._path` と同じ理由）。
        if not upload_id or "/" in upload_id or upload_id.startswith("."):
            raise UploadSessionError(f"invalid upload id: {upload_id!r}")
        return self.root / f"{upload_id}.part", self.root / f"{upload_id}.json"

    # -- 作る・訊く --------------------------------------------------------

    def create(
        self,
        *,
        upload_id: str,
        tenant_id: str,
        learner_id: str,
        task_version_id: str,
        filename: str,
        max_bytes: int,
    ) -> UploadSession:
        data, meta = self._paths(upload_id)
        created = datetime.now(UTC)
        meta.write_text(
            json.dumps(
                {
                    "tenant_id": tenant_id,
                    "learner_id": learner_id,
                    "task_version_id": task_version_id,
                    "filename": filename,
                    "max_bytes": max_bytes,
                    "created_at": created.isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        data.touch()
        return UploadSession(
            id=upload_id,
            tenant_id=tenant_id,
            learner_id=learner_id,
            task_version_id=task_version_id,
            filename=filename,
            max_bytes=max_bytes,
            created_at=created,
            offset=0,
        )

    def get(self, upload_id: str) -> UploadSession:
        data, meta = self._paths(upload_id)
        if not meta.is_file() or not data.is_file():
            raise UploadSessionError("このアップロードは見つかりません")
        given = json.loads(meta.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(given["created_at"])
        if datetime.now(UTC) - created > self._ttl:
            raise UploadSessionError("このアップロードは期限切れです")
        return UploadSession(
            id=upload_id,
            tenant_id=given["tenant_id"],
            learner_id=given["learner_id"],
            task_version_id=given["task_version_id"],
            filename=given["filename"],
            max_bytes=int(given["max_bytes"]),
            created_at=created,
            offset=data.stat().st_size,
        )

    # -- 書く・確定する ----------------------------------------------------

    def append(self, upload_id: str, chunks: Iterable[bytes], *, offset: int) -> int:
        """続きを書き、書いたあとのオフセットを返す。

        **位置が合わなければ書かない。** ずれたまま書くと、穴が空いたり
        重なったりした動画が「正常に受け付けられた」ことになる。
        """
        session = self.get(upload_id)
        if offset != session.offset:
            raise OffsetMismatch(session.offset)
        data, _ = self._paths(upload_id)
        written = session.offset
        with data.open("ab") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                if written + len(chunk) > session.max_bytes:
                    # **ここで止める。** 受けてから断ると、上限を超えた
                    # ぶんだけディスクに書いたことになる。
                    handle.flush()
                    os.fsync(handle.fileno())
                    raise TooLarge(f"ファイルが大きすぎます（上限 {session.max_bytes} バイト）")
                handle.write(chunk)
                written += len(chunk)
            handle.flush()
            # **切れても届いたぶんは残す。** ここで同期しておかないと、
            # 機械が落ちたときに「サーバは持っている」と答えた位置まで
            # 戻れない（`offset` は実サイズから引くので、嘘にはならないが
            # 学習者は送り直しになる）。
            os.fsync(handle.fileno())
        return written

    def path_of(self, upload_id: str) -> Path:
        """確定のために中身の場所を渡す（`FilesystemArtifactStore.adopt` へ）。"""
        data, _ = self._paths(upload_id)
        if not data.is_file():
            raise UploadSessionError("このアップロードは見つかりません")
        return data

    def discard(self, upload_id: str) -> None:
        """受け皿を片付ける。**確定の後にも、諦めたときにも呼ぶ。**"""
        data, meta = self._paths(upload_id)
        data.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    # -- 掃除 --------------------------------------------------------------

    def sweep(self) -> int:
        """期限を過ぎた受け皿を消し、消した数を返す。

        **専用のタイマーを足さない。** セッションを作るときに 1 回呼ぶ
        （数 GB を送り始める人が、数十件の `stat` を払う）。掃除のためだけに
        unit を増やすと、その unit が止まったことに誰も気づかない。
        """
        removed = 0
        now = datetime.now(UTC)
        for meta in self.root.glob("*.json"):
            try:
                created = datetime.fromisoformat(
                    json.loads(meta.read_text(encoding="utf-8"))["created_at"]
                )
            except Exception:
                # 読めない受け皿は、古いものとして扱う（残しても使えない）。
                created = datetime.fromtimestamp(meta.stat().st_mtime, UTC)
            if now - created > self._ttl:
                self.discard(meta.stem)
                removed += 1
        return removed


__all__ = [
    "DEFAULT_TTL",
    "INCOMPLETE_DIR",
    "FilesystemUploadSessions",
    "OffsetMismatch",
    "TooLarge",
    "UploadSession",
    "UploadSessionError",
]
