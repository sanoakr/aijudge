"""テストを並列に流すときの、PostgreSQL の分け方。

CI はテストを `pytest -n auto`（pytest-xdist）で並列に流す。1 プロセスで
流すと 15〜30 分かかり、その大半が待ち時間だったため。

**PostgreSQL を使うテストは、同じ DB の表や schema を消しては作り直す**
（`test_repositories.py` の `drop_all`・`test_migrations.py` の
`DROP SCHEMA public CASCADE` など 9 ファイル）。並列の worker が 1 つの DB を
共有すると、別の worker のテストの最中に表が消える。

そこで worker ごとに DB を作り（`<元の DB 名>_gw0` など）、
`AIJUDGE_TEST_DATABASE_URL` をその DB に向け直す。テストファイルは読み込み時に
この変数を読む（`POSTGRES_URL = os.environ.get(...)`）ので、読み込みより前に
呼ばれる `pytest_configure` で差し替える ── 9 ファイルは何も変えずに済む。

並列でないとき（手元の既定）と、変数が無いときは何もしない。
"""

from __future__ import annotations

import os

import pytest

ENV_TEST_DATABASE_URL = "AIJUDGE_TEST_DATABASE_URL"
#: pytest-xdist が worker の中で立てる変数（`gw0`, `gw1`, …）。
ENV_XDIST_WORKER = "PYTEST_XDIST_WORKER"


def pytest_configure(config: pytest.Config) -> None:
    worker = os.environ.get(ENV_XDIST_WORKER)
    base = os.environ.get(ENV_TEST_DATABASE_URL, "")
    if not worker or not base.startswith("postgresql"):
        return
    os.environ[ENV_TEST_DATABASE_URL] = _database_for_worker(base, worker)


def _database_for_worker(base: str, worker: str) -> str:
    """worker 用の DB を作り直し、その URL を返す。

    **毎回まっさらにする。** 前の実行が途中で止まると表が残り、
    `test_migrations.py`（マイグレーションだけで作った schema とモデルを
    比べる）が残りに引きずられる。
    """
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url

    url = make_url(base)
    name = f"{url.database}_{worker}"
    # CREATE / DROP DATABASE はトランザクションの中では流せない。
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            # 名前は `<設定の DB 名>_gw<n>` で、外からの入力を含まない。
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return url.set(database=name).render_as_string(hide_password=False)
