"""接続とトランザクション境界。

**トランザクションは 1 提出につき 1 つ。** 提出・ジョブ・イベントが同時に
成立しなければならない（片方だけ成立すると、採点されない提出か存在しない
提出の採点が生まれる）。`SqlUnitOfWork` がその境界を持つ。

Phase 0 の既定は PostgreSQL。SQLite も動くが、**行ロックが無い**ので
複数ワーカーでの取得が二重配布になりうる。単一プロセスの開発用に限る。
その違いを黙って隠さず、`supports_row_locking` で申告する
（S4 のバックエンドが `limitations` を申告するのと同じ考え方）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .identity_repository import SqlIdentityRepository
from .repositories import (
    SqlGradingRunRepository,
    SqlJobQueue,
    SqlOutbox,
    SqlReviewRepository,
    SqlSubmissionRepository,
    SqlTaskRepository,
)
from .schema import Base
from .skill_repository import SqlSkillRepository

ENV_DATABASE_URL = "AIJUDGE_DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://aijudge:aijudge@localhost:5432/aijudge"


def database_url() -> str:
    return os.environ.get(ENV_DATABASE_URL, DEFAULT_DATABASE_URL)


def supports_row_locking(engine: Engine) -> bool:
    """このバックエンドが行ロックを持つか。

    持たないと、複数ワーカーが同じジョブを取って同じ提出を二度採点する。
    偽の環境で採点ワーカーを複数立てないこと。
    """
    return engine.dialect.name != "sqlite"


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    resolved = url or database_url()
    if resolved.startswith("sqlite"):
        # インメモリ SQLite は接続ごとに別 DB になるため、接続を共有する。
        from sqlalchemy.pool import StaticPool

        return create_engine(
            resolved,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    # 締切前のバーストで接続が枯れないよう、事前に少し多めに張る。
    return create_engine(resolved, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=20)


def create_schema(engine: Engine) -> None:
    """スキーマを作る。

    Phase 0 は `create_all` で足りる。最初のスキーマ変更で Alembic に移す
    （運用中のデータがある状態で `create_all` は使えない）。それまでは
    「壊して作り直せる」段階だと明示しておく。
    """
    Base.metadata.create_all(engine)


class SqlUnitOfWork:
    """1 トランザクション。

    `with` を抜けるときに commit していなければロールバックする。
    「保存したつもりで消えている」より「保存されていないことが分かる」方がまし。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> SqlUnitOfWork:
        self._session = self._session_factory()
        self.submissions = SqlSubmissionRepository(self._session)
        self.runs = SqlGradingRunRepository(self._session)
        self.jobs = SqlJobQueue(self._session)
        self.outbox = SqlOutbox(self._session)
        self.tasks = SqlTaskRepository(self._session)
        self.reviews = SqlReviewRepository(self._session)
        self.identity = SqlIdentityRepository(self._session)
        self.skills = SqlSkillRepository(self._session)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._session is None:  # pragma: no cover - __enter__ 前に抜けることはない
            return
        try:
            # commit されていなければ捨てる。中途半端に残さない。
            self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("the unit of work is not open; use it as a context manager")
        return self._session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class Database:
    """接続と UnitOfWork の工場。アプリはこれを 1 つ持つ。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session_factory = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def connect(cls, url: str | None = None, *, create: bool = False) -> Database:
        engine = create_db_engine(url)
        database = cls(engine)
        if create:
            create_schema(engine)
        return database

    @property
    def supports_row_locking(self) -> bool:
        return supports_row_locking(self.engine)

    def unit_of_work(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._session_factory)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
