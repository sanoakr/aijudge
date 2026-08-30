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

日時は必ず timezone 付きで扱う。素の TIMESTAMP に入れると、締切判定が
サーバのローカル時刻に依存する。**ただしバックエンドによっては保証されない**
（SQLite は tzinfo を落とす）。方言差を呼び出し側に漏らさないため、
`UtcDateTime` が読み書きの両方で UTC を強制する。
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# PostgreSQL では JSONB（索引が張れる）、それ以外では JSON。
JsonType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator):
    """常に timezone 付きの UTC で読み書きする日時。

    PostgreSQL の `TIMESTAMP WITH TIME ZONE` は aware な値を返すが、
    **SQLite は tzinfo を落として naive を返す**。そのまま aware な値と
    比較すると `TypeError` になり、比較できてしまう経路（naive 同士）では
    サーバのローカル時刻で締切を判定することになる。

    どちらも受け入れられないので、方言差をここで吸収する。書き込み時に
    naive を拒否するのは、`datetime.now()` を `datetime.now(UTC)` の
    代わりに使った誤りを、静かに通さず落とすため。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetimes are not accepted; use datetime.now(UTC) so that "
                "deadlines do not depend on the server's local time"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite。保存時に UTC へ正規化してあるので、UTC として復元する。
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


Timestamp = UtcDateTime()


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


class HumanReviewRow(Base):
    """教員の確認・修正。

    **GradingRun を書き換えない。** 追記でしか記録しない（P8）。この行の
    存在が成績の確定を意味する。
    """

    __tablename__ = "human_reviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grading_run_id: Mapped[str] = mapped_column(String(64), index=True)
    submission_id: Mapped[str] = mapped_column(String(64), index=True)
    grader_id: Mapped[str] = mapped_column(String(64), index=True)
    agreed: Mapped[bool] = mapped_column(Boolean, index=True)
    reviewed_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 1 採点につき確定は 1 つ。二度確定できると成績が二つ存在する。
        UniqueConstraint("grading_run_id", name="uq_reviews_run"),
    )


class FinalizationRow(Base):
    """成績が確定した事実。**追記のみ。**

    `human_reviews` とは別の表にする。あちらは「教員がその 1 件を読んだ」
    記録で、一致度の測定が証拠に使う。確定は自動でも一括でも起きるので、
    同じ表に混ぜると誰も読んでいない提出に教員の同意が記録される
    （ADR 0005 / ADR 0010）。

    `source` を列にしているのは、自動確定の件数を運用中に数えるため。
    """

    __tablename__ = "finalizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grading_run_id: Mapped[str] = mapped_column(String(64), index=True)
    submission_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    # 確定させた人。自動確定では NULL。
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    finalized_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 1 採点につき確定は 1 つ。二度確定できると成績が二つ存在する。
        UniqueConstraint("grading_run_id", name="uq_finalizations_run"),
    )


class ReviewRequestRow(Base):
    """学習者からの再確認の依頼。

    AI の判定は採点直後に学習者へ示すので、誤りを疑ったときの導線が要る
    （設計方針 §9.4「異議申し立て導線」）。1 採点につき 1 件。
    """

    __tablename__ = "review_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grading_run_id: Mapped[str] = mapped_column(String(64), index=True)
    submission_id: Mapped[str] = mapped_column(String(64), index=True)
    learner_id: Mapped[str] = mapped_column(String(64), index=True)
    requested_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    # 対応した教員レビュー。NULL なら未対応 = 教員の待ち行列に出る。
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 同じ採点に二重に依頼を出させない。
        UniqueConstraint("grading_run_id", name="uq_requests_run"),
    )


class BlindMarkRow(Base):
    """教員が AI を見る前に付けた段階（測定用の正解データ）。

    提出 1 件につき 1 つ。二度目を受け付けると、AI を見たあとの段階で
    上書きできてしまい正解データが汚れる（ADR 0005）。
    """

    __tablename__ = "blind_marks"

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grader_id: Mapped[str] = mapped_column(String(64), index=True)
    marked_at: Mapped[datetime] = mapped_column(Timestamp)
    document: Mapped[dict] = mapped_column(JsonType)


class GradingJobRow(Base):
    __tablename__ = "grading_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    submission_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_profile: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(32))
    # 段階。**列にする。** ワーカーがここで絞って取るので、JSON の中では引けない。
    phase: Mapped[str] = mapped_column(String(32), index=True, default="deterministic")
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
        # 段階を絞るワーカーの取得クエリ。
        Index("ix_jobs_phase_available", "phase", "state", "available_at"),
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


class UserRow(Base):
    """利用者。

    パスワードハッシュはここに置く。**この列をアプリ層へ出さない**
    （S1 の `Principal` が外向きの型で、資格情報を含まない）。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    login: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp)

    __table_args__ = (
        # 同じテナント内でログイン ID は一意。別テナントでは衝突してよい
        # （機関をまたいで学籍番号が重なるのは普通のこと）。
        UniqueConstraint("tenant_id", "login", name="uq_users_tenant_login"),
    )


class SessionRow(Base):
    """ログインセッション。

    保存するのはトークンの**ハッシュ**。DB が漏れてもセッションを
    乗っ取れないようにするため。
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    expires_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)

    __table_args__ = (
        # トークンからセッションを引くのが毎リクエストの操作。
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )


class ApiTokenRow(Base):
    """非対話の呼び出し元の資格情報。

    **セッションとは別の表にする。** 用途が違えば寿命も違い、同じ表に混ぜると
    セッションの有効期限を長くするか、トークンを毎日作り直すかの二択になる。

    セッションと同じく**ハッシュだけを保存する**。平文は発行時に一度返して
    以降どこにも残らないので、DB が漏れても API を叩けない。
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    # 引くための鍵。乱数なので辞書攻撃の対象にならず、ソルトは付けない。
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    note: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True, index=True)
    # 使われていないトークンを見つけて消すため。
    last_used_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)


class CourseRow(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256))
    term: Mapped[str] = mapped_column(String(64), index=True)
    subject_profile: Mapped[str] = mapped_column(String(64), index=True)
    # コースの概要・到達目標（Markdown）。シラバスから写して置く。
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 締切から何分で成績を自動確定するか。NULL なら自動確定しない。
    auto_finalize_after_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # この科目の既定の提出ファイル形式（`[".c", ".pdf"]`）。NULL・空なら
    # 組み込みの既定。**課題ごとの指定が上書きする**（uploads.py）。
    upload_suffixes: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    # 遅延の減点の段（`[{"after_hours": 24, "ratio": 0.3}, ...]`）。
    # NULL・空なら遅延を見ない。**評価器の設定ではない**（評価と独立）。
    late_penalty_steps: Mapped[list | None] = mapped_column(JsonType, nullable=True)

    __table_args__ = (UniqueConstraint("tenant_id", "code", "term", name="uq_courses_code_term"),)


class EnrollmentRow(Base):
    """受講。

    「見えてはいけないものが見えない」の根拠になる表。UI で隠すのは表示の
    都合であって権限ではない。
    """

    __tablename__ = "enrollments"

    course_id: Mapped[str] = mapped_column(String(64), ForeignKey("courses.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32), index=True)

    __table_args__ = (Index("ix_enrollments_user", "tenant_id", "user_id"),)


class TaskRow(Base):
    """課題。

    テナントは持たない。`Task` はコースに属し、コースがテナントに属する
    （core/tenancy.py）。ここに tenant_id を複製すると、コースの移動時に
    片方だけ更新される余地が生まれる。テナントで絞るときはコース経由で引く。
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(64), index=True)
    # どの問題セットか。一覧の階層化と並べ替えに使うので列にする
    # （JSON の中だと並べ替えられない）。
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (Index("ix_tasks_course_order", "course_id", "session", "position"),)


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


class KnowledgeComponentRow(Base):
    """知識要素（KC）。

    正準キー（`namespace.path…`）で一意。**ID はキーから導出される**ので、
    課題が KC を名指ししてから体系に足しても対応が繋がる。
    """

    __tablename__ = "knowledge_components"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(256), index=True)
    label: Mapped[str] = mapped_column(String(256))
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (UniqueConstraint("key", name="uq_kc_key"),)


class SkillStateRow(Base):
    """学習者 × KC の習熟度（S7）。

    **採点結果とは別の表である。** 採点は追記のみだが、習熟度は最新の 1 行を
    持ち替える推定値で、根拠（`SkillEvidence`）を通じて採点結果を指す。
    """

    __tablename__ = "skill_states"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    learner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mastery: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(32))
    observation_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # 「この学習者の習熟度一覧」— ポートフォリオ（S8）が使う。
        Index("ix_skill_tenant_learner", "tenant_id", "learner_id"),
    )


class TaskChecksRow(Base):
    """課題版に対して走らせた検査（門・解答可能性）の結果。

    **課題版とは別の表にする。** 課題版は公開後不変（P8）だが、検査は
    門を直すたびに走らせ直せる。同じ行に置くと、測り直しが課題の改変に見える。
    """

    __tablename__ = "task_checks"

    task_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    usable: Mapped[bool] = mapped_column(Boolean, index=True)
    checked_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    document: Mapped[dict] = mapped_column(JsonType)


class TaskEmbeddingRow(Base):
    """課題文の埋め込み。重複検出に使う。

    **ベクトルは JSON で持つ。** pgvector は索引であって能力ではなく、
    1 コース数十〜数百件の規模では全件とのコサインの方が速い。索引が要る
    規模になったらこの列を `vector` 型に替える ── 比較そのものは
    `aijudge_authoring.similarity` にあり、保存先を知らない。

    **モデル名と次元を持つ。** 埋め込みモデルを替えたら過去のベクトルとは
    比較できない（次元が同じでも意味空間が違う）。混ぜると、無関係な課題が
    似ていることになる。
    """

    __tablename__ = "task_embeddings"

    task_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject_profile: Mapped[str] = mapped_column(String(64), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list] = mapped_column(JsonType)
