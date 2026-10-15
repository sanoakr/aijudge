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

**テストは本物の LLM に届かせない**（#616）。LLM の向き先は既定で
`localhost:11434` で、CI には何も無いので接続がすぐ拒否されて先へ進むが、
手元で LLM が動いていると本当に生成を待つ（2026-10-15、PAIR のプロキシに
置き換わってから `pytest` が終わらなくなった）。結果が「手元で LLM が動いて
いるか」で変わるので、`AIJUDGE_LIVE_LLM=1` を明示したとき以外は向き先を
閉じたポートに差し替え、従系は外す。シェルで export された値も上書きする。
"""

from __future__ import annotations

import os

import pytest

ENV_TEST_DATABASE_URL = "AIJUDGE_TEST_DATABASE_URL"
#: pytest-xdist が worker の中で立てる変数（`gw0`, `gw1`, …）。
ENV_XDIST_WORKER = "PYTEST_XDIST_WORKER"
#: 本物の LLM を使うテスト（`evals/test_llm_live.py`）を走らせるときだけ "1" にする。
ENV_LIVE_LLM = "AIJUDGE_LIVE_LLM"
#: 何も待ち受けていないポート。接続は CI と同じくすぐ拒否される。
CLOSED_LLM_URL = "http://127.0.0.1:9"
#: 主系の向き先。閉じたポートへ差し替える。
LLM_URL_VARIABLES = ("AIJUDGE_LLM_BASE_URL", "AIJUDGE_LLM_VISION_BASE_URL")
#: 従系の向き先。残すと主系が拒否されたあとにそちらへ回るので、外す。
LLM_FALLBACK_VARIABLES = (
    "AIJUDGE_LLM_FALLBACK_BASE_URL",
    "AIJUDGE_LLM_VISION_FALLBACK_BASE_URL",
)


def pytest_configure(config: pytest.Config) -> None:
    _keep_tests_off_real_llms()
    worker = os.environ.get(ENV_XDIST_WORKER)
    base = os.environ.get(ENV_TEST_DATABASE_URL, "")
    if not worker or not base.startswith("postgresql"):
        return
    os.environ[ENV_TEST_DATABASE_URL] = _database_for_worker(base, worker)


def _keep_tests_off_real_llms() -> None:
    """LLM の向き先を閉じたポートにする（#616）。worker の中でも呼ばれる。"""
    if os.environ.get(ENV_LIVE_LLM) == "1":
        return
    for name in LLM_URL_VARIABLES:
        os.environ[name] = CLOSED_LLM_URL
    for name in LLM_FALLBACK_VARIABLES:
        os.environ.pop(name, None)


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
