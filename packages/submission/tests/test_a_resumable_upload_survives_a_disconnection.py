"""中断したところから続けられるアップロードの受け皿（#119）。

固定したいのは 6 つ。

続きから       切れた位置を訊いて、その続きを書ける。送り直しは差分だけ。
位置が合わねば断る  ずれたまま書くと、穴が空いた動画が「正常」として残る。
実サイズで答える    オフセットは別に数えず、ファイルの実サイズから引く。
上限は通算で見る    分割をまたいで超えたら、そこで止める。
持ち主             誰のアップロードかを受け皿が持つ（id を知るだけでは書けない）。
掃除               期限を過ぎた受け皿は消える。数 GB が積み上がらない。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aijudge_submission import (
    FilesystemArtifactStore,
    FilesystemUploadSessions,
    OffsetMismatch,
    TooLarge,
    UploadSessionError,
)

TENANT = "ten_" + "0" * 32
LEARNER = "usr_" + "1" * 32
TASK = "tsv_" + "2" * 32


def _sessions(tmp_path: Path, **kwargs) -> FilesystemUploadSessions:
    return FilesystemUploadSessions(tmp_path / "video", **kwargs)


def _create(sessions: FilesystemUploadSessions, *, max_bytes: int = 1000, upload_id="upl_a"):
    return sessions.create(
        upload_id=upload_id,
        tenant_id=TENANT,
        learner_id=LEARNER,
        task_version_id=TASK,
        filename="demo.mp4",
        max_bytes=max_bytes,
    )


def test_a_broken_upload_continues_from_where_it_stopped(tmp_path: Path) -> None:
    """**これがこの仕組みの目的。** 90% で切れても、送り直すのは残りだけ。"""
    sessions = _sessions(tmp_path)
    _create(sessions)

    sessions.append("upl_a", [b"first-half"], offset=0)
    # ここで切断したとする。クライアントは位置を訊き直す。
    assert sessions.get("upl_a").offset == len(b"first-half")

    end = sessions.append("upl_a", [b"second-half"], offset=len(b"first-half"))

    assert end == len(b"first-halfsecond-half")
    assert sessions.path_of("upl_a").read_bytes() == b"first-halfsecond-half"


def test_a_wrong_offset_is_refused_and_says_where_the_server_is(tmp_path: Path) -> None:
    """**ずれたまま書かない。** 穴や重なりのある動画が「正常」になる。"""
    sessions = _sessions(tmp_path)
    _create(sessions)
    sessions.append("upl_a", [b"0123456789"], offset=0)

    with pytest.raises(OffsetMismatch) as raised:
        sessions.append("upl_a", [b"xxx"], offset=3)

    # クライアントはここから送り直せばよい（やり直しではない）。
    assert raised.value.offset == 10
    assert sessions.path_of("upl_a").read_bytes() == b"0123456789"


def test_the_offset_comes_from_the_file_itself(tmp_path: Path) -> None:
    """別に数えて持つと、書き込みが落ちたとき「記録上は届いている」が起きる。"""
    sessions = _sessions(tmp_path)
    _create(sessions)
    sessions.append("upl_a", [b"abc"], offset=0)

    # 受け皿を直接削った状態＝書き込みが落ちた状態を作る。
    path = sessions.path_of("upl_a")
    path.write_bytes(b"a")

    assert sessions.get("upl_a").offset == 1


def test_the_limit_is_counted_across_chunks(tmp_path: Path) -> None:
    """分割すれば通る、では上限の意味が無い。"""
    sessions = _sessions(tmp_path)
    _create(sessions, max_bytes=8)
    sessions.append("upl_a", [b"1234"], offset=0)

    with pytest.raises(TooLarge):
        sessions.append("upl_a", [b"56789"], offset=4)


def test_the_receipt_knows_whose_it_is(tmp_path: Path) -> None:
    """id を知っているだけでは、他人の提出に追記できない。"""
    sessions = _sessions(tmp_path)
    _create(sessions)

    session = sessions.get("upl_a")

    assert session.belongs_to(tenant_id=TENANT, learner_id=LEARNER)
    assert not session.belongs_to(tenant_id=TENANT, learner_id="usr_" + "9" * 32)


def test_an_expired_receipt_is_not_offered(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path, ttl=timedelta(seconds=-1))
    _create(sessions)

    with pytest.raises(UploadSessionError):
        sessions.get("upl_a")


def test_sweeping_removes_what_nobody_came_back_for(tmp_path: Path) -> None:
    """**締切をまたいで残さない。** 諦めた数 GB が積み上がる。"""
    sessions = _sessions(tmp_path, ttl=timedelta(seconds=-1))
    _create(sessions)
    _create(sessions, upload_id="upl_b")

    removed = sessions.sweep()

    assert removed == 2
    assert list(sessions.root.glob("*")) == []


def test_an_id_that_escapes_the_directory_is_refused(tmp_path: Path) -> None:
    """**作る側が正しいから安全**は保証ではない（ストアと同じ理由）。"""
    sessions = _sessions(tmp_path)

    with pytest.raises(UploadSessionError):
        sessions.get("../../etc/passwd")


def test_finishing_moves_the_bytes_without_copying_them(tmp_path: Path) -> None:
    """確定は `os.replace`。**3 GB を読み直して書き直さない。**

    ハッシュは確定の 1 回だけ読んで計算する（`hashlib` の途中状態は
    保存できないので、リクエストをまたいで積み上げる道が無い）。
    """
    root = tmp_path / "video"
    sessions = _sessions(tmp_path)
    store = FilesystemArtifactStore(root)
    _create(sessions)
    sessions.append("upl_a", [b"video-bytes"], offset=0)
    source = sessions.path_of("upl_a")

    blob = store.adopt("t/sub/art/demo.mp4", source)

    assert blob.byte_size == len(b"video-bytes")
    assert store.get("t/sub/art/demo.mp4") == b"video-bytes"
    # 受け皿は移動したので残らない（コピーではない）。
    assert not source.exists()
