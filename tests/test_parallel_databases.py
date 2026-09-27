"""並列の worker が PostgreSQL の DB を共有していないこと（直下の `conftest.py`）。

共有すると、ある worker のテストの最中に別の worker が表を消す。症状は
「たまに落ちる」で、原因に辿り着きにくい ── だから並列で流すときは、
向け直しが効いていることをここで先に確かめる。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url

WORKER = os.environ.get("PYTEST_XDIST_WORKER")
URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL", "")


@pytest.mark.skipif(
    not (WORKER and URL.startswith("postgresql")),
    reason="PostgreSQL を並列で流すときだけ（-n と AIJUDGE_TEST_DATABASE_URL）",
)
def test_each_worker_has_its_own_database() -> None:
    database = make_url(URL).database
    assert database is not None
    assert database.endswith(f"_{WORKER}")
