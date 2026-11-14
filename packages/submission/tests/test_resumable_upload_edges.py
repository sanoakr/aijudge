"""分割アップロードの受け皿の境界を固定する（#686）。

`test_a_resumable_upload_survives_a_disconnection.py` が見ているのは各規則の
片側だけで、ここでは逆側を固定する。

掃除     期限内の受け皿は残す。`sweep` は受け皿を作るたびに呼ばれるので
         （studentweb の `POST /tasks/{tv}/uploads`）、全部消すと、ほかの学習者が
         送信中の数 GB が消え、次の PATCH で 404 になって最初から送り直しになる。
上限     ちょうど上限は受け付ける。最後のチャンクで 413 になると、そこまで
         送ったバイトを捨てることになる。
持ち主   テナントが違えば、learner_id が同じでも本人ではない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_submission import FilesystemUploadSessions, TooLarge

TENANT = "ten_" + "0" * 32
OTHER_TENANT = "ten_" + "8" * 32
LEARNER = "usr_" + "1" * 32
TASK = "tsv_" + "2" * 32


def _sessions(tmp_path: Path, **kwargs) -> FilesystemUploadSessions:
    return FilesystemUploadSessions(tmp_path / "video", **kwargs)


def _create(sessions: FilesystemUploadSessions, upload_id: str, *, max_bytes: int = 1000):
    return sessions.create(
        upload_id=upload_id,
        tenant_id=TENANT,
        learner_id=LEARNER,
        task_version_id=TASK,
        filename="demo.mp4",
        max_bytes=max_bytes,
    )


def _age(sessions: FilesystemUploadSessions, upload_id: str, by: timedelta) -> None:
    """受け皿の作成時刻を過去へずらす（期限切れを作る）。"""
    meta = sessions.root / f"{upload_id}.json"
    given = json.loads(meta.read_text(encoding="utf-8"))
    given["created_at"] = (datetime.now(UTC) - by).isoformat()
    meta.write_text(json.dumps(given), encoding="utf-8")


# --------------------------------------------------------------------------
# 掃除
# --------------------------------------------------------------------------


def test_sweeping_keeps_an_upload_still_in_progress(tmp_path: Path) -> None:
    """既定の期限（24 時間）の中にある受け皿は消さない。"""
    sessions = _sessions(tmp_path)
    _create(sessions, "upl_a")
    sessions.append("upl_a", [b"first-half"], offset=0)

    removed = sessions.sweep()

    assert removed == 0
    # 送信中の学習者は続きを書ける。
    assert sessions.get("upl_a").offset == len(b"first-half")


def test_sweeping_removes_only_the_expired_ones(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path, ttl=timedelta(hours=1))
    _create(sessions, "upl_old")
    _create(sessions, "upl_new")
    _age(sessions, "upl_old", timedelta(hours=2))

    removed = sessions.sweep()

    assert removed == 1
    assert sorted(path.name for path in sessions.root.iterdir()) == [
        "upl_new.json",
        "upl_new.part",
    ]


# --------------------------------------------------------------------------
# 上限の境界
# --------------------------------------------------------------------------


def test_exactly_the_limit_is_accepted_across_chunks(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _create(sessions, "upl_a", max_bytes=8)
    sessions.append("upl_a", [b"1234"], offset=0)

    end = sessions.append("upl_a", [b"5678"], offset=4)

    assert end == 8
    assert sessions.path_of("upl_a").read_bytes() == b"12345678"


def test_one_byte_over_the_limit_is_refused(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _create(sessions, "upl_a", max_bytes=8)
    sessions.append("upl_a", [b"12345678"], offset=0)

    with pytest.raises(TooLarge):
        sessions.append("upl_a", [b"9"], offset=8)
    # 超えたぶんは書かない。
    assert sessions.get("upl_a").offset == 8


# --------------------------------------------------------------------------
# 持ち主
# --------------------------------------------------------------------------


def test_the_same_learner_id_in_another_tenant_is_not_the_owner(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _create(sessions, "upl_a")

    session = sessions.get("upl_a")

    assert session.belongs_to(tenant_id=TENANT, learner_id=LEARNER)
    assert not session.belongs_to(tenant_id=OTHER_TENANT, learner_id=LEARNER)
