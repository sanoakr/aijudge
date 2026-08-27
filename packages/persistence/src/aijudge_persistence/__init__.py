"""aiJudge persistence — 保存先の実装。

インフラ層であって、サブシステムではない。S2 / S3 が定義したプロトコルを
PostgreSQL とオブジェクトストレージで実装する。**逆向きの依存は無い**
（サブシステムはこのパッケージを知らない）。それが「保存先を差し替えられる」
の意味で、`.importlinter` の契約で保証している。

Phase 0 の既定は PostgreSQL + ファイルシステム。MinIO（S3 互換）は
`ObjectArtifactStore` に差し替えるだけで移る。
"""

from __future__ import annotations

from .engine import (
    DEFAULT_DATABASE_URL,
    ENV_DATABASE_URL,
    Database,
    SqlUnitOfWork,
    create_db_engine,
    create_schema,
    database_url,
    supports_row_locking,
)
from .identity_repository import SqlIdentityRepository
from .objectstore import ObjectArtifactStore
from .observations import OBSERVATIONS_DIR, ObservationFileStore
from .repositories import (
    SqlGradingRunRepository,
    SqlJobQueue,
    SqlOutbox,
    SqlSubmissionRepository,
    SqlTaskRepository,
)
from .schema import Base

__all__ = [
    "DEFAULT_DATABASE_URL",
    "ENV_DATABASE_URL",
    "OBSERVATIONS_DIR",
    "Base",
    "Database",
    "ObjectArtifactStore",
    "ObservationFileStore",
    "SqlGradingRunRepository",
    "SqlIdentityRepository",
    "SqlJobQueue",
    "SqlOutbox",
    "SqlSubmissionRepository",
    "SqlTaskRepository",
    "SqlUnitOfWork",
    "create_db_engine",
    "create_schema",
    "database_url",
    "supports_row_locking",
]
