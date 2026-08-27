"""データベーススキーマ。

**集約は JSON で持つ。** `GradingRun` は観点別スコア・根拠・評価器の生出力を
含む木で、常に丸ごと読み書きされる。これを正規化しても得るものが無い
（測定は SQL ではなく観測レコードを読む、ADR 0007）。正規化が必要になるのは
SQL で集約の中身を検索するようになったときで、そのときに移す。

一方で**検索と一意性の条件になる列は必ず列にする**。JSON の中に入れると
索引が張れず、冪等キーの一意制約も効かない。

    submissions        提出。artifacts は JSON（丸ごと読む）
    submission_keys    冪等キー → 提出。UNIQUE 制約が二重投入を止める最後の砦
    grading_runs       採点結果。**追記のみ**（P8）
    grading_jobs       採点ジョブ。available_at と state に索引
    outbox_events      ドメインイベント。published_at が NULL なら未送信
    tasks / task_versions  課題。公開後は不変

日時は必ず timezone 付きで持つ。素の TIMESTAMP に入れると、締切判定が
サーバのローカル時刻に依存する。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# PostgreSQL では JSONB（索引が張れる）、それ以外では JSON。
JsonType = JSON().with_variant(JSONB(), "postgresql")
Timestamp = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class SubmissionRow(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    task_version_id: Mapped[str] = mapped_column(String(64), index=True)
    learner_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    submitted_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    # Submission 全体（artifacts を含む）。読むときは丸ごと。
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 「この学習者のこの課題の提出一覧」が最頻のクエリ。
        Index("ix_submissions_learner_task", "tenant_id", "learner_id", "task_version_id"),
    )


class SubmissionKeyRow(Base):
    """冪等キー → 提出。

    UNIQUE 制約が二重投入を止める最後の砦。アプリ側の「あれば返す」だけでは、
    同時に来た 2 つのリクエストが両方「無い」を見て両方作る。
    """

    __tablename__ = "submission_keys"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(64), ForeignKey("submissions.id"), index=True)


class GradingRunRow(Base):
    """採点結果。**追記のみ。**

    `superseded_by` だけが後から書かれる。再採点が生まれて初めて確定する値で、
    旧採点の側に書くしかない（protocols.GradingRunRepository.supersede 参照）。
    """

    __tablename__ = "grading_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(64), index=True)
    task_version_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_profile: Mapped[str] = mapped_column(String(64), index=True)
    input_hash: Mapped[str] = mapped_column(String(128), index=True)
    score_ratio: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    routing: Mapped[str] = mapped_column(String(32), index=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 「この提出の最新の採点」— レビューと学生の結果表示が使う。
        Index("ix_runs_submission_created", "submission_id", "created_at"),
    )


class GradingJobRow(Base):
    __tablename__ = "grading_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    submission_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_profile: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(192))
    state: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(Timestamp)
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    updated_at: Mapped[datetime] = mapped_column(Timestamp)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 二重投入で GPU を二度回さないための一意制約。
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        # ワーカーの取得クエリ。state と available_at で絞って古い順に取る。
        Index("ix_jobs_available", "state", "available_at"),
        Index("ix_jobs_profile_state", "subject_profile", "state"),
    )


class OutboxRow(Base):
    """ドメインイベントの送信箱。

    `published_at` が NULL なら未送信。リレーがここを読んで流す。
    提出と同じトランザクションで書くので、「保存できたのにイベントが出ない」
    が起きない。
    """

    __tablename__ = "outbox_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    published_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (Index("ix_outbox_pending", "published_at", "occurred_at"),)


class TaskRow(Base):
    """課題。

    テナントは持たない。`Task` はコースに属し、コースがテナントに属する
    （core/tenancy.py）。ここに tenant_id を複製すると、コースの移動時に
    片方だけ更新される余地が生まれる。テナントで絞るときはコース経由で引く。
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[dict] = mapped_column(JsonType)


class TaskVersionRow(Base):
    """課題版。公開後は不変（P8）。採点の再現性の根拠になる。"""

    __tablename__ = "task_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    subject_profile: Mapped[str] = mapped_column(String(64), index=True)
    review_state: Mapped[str] = mapped_column(String(32), index=True)
    allow_handwriting: Mapped[bool] = mapped_column(Boolean, default=False)
    statement: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        UniqueConstraint("task_id", "version", name="uq_task_version"),
        Index("ix_task_versions_task_version", "task_id", "version"),
    )
