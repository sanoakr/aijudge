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
    audit_events       誰が成績に届く何を変えたか。**追記のみ**（ADR 0016）
    run_requests       IDE の試しの実行。採点キューとは別（ADR 0024）。結果は残さない
    ide_buffers        IDE の自動保存。(学習者, 課題) ごとに 1 行、上書き
    ide_submission_links  IDE からの提出の出どころ（本人か、受付終了時の自動提出か）

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
    text,
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
    # 成績にも測定にも数えない提出か（#108・#194）。
    #
    # **これは `Submission.is_trial` の写しであって、定義ではない。** 定義は
    # 模型にあり（`submitted_as is not LEARNER or is_demo`）、ここは SQL から
    # 引けるようにするための索引である。保存のときに模型から書き、**手で
    # 編集しない**。
    #
    # 列にしてある理由は、無いあいだに同じ規則が 3 通りに写されたからである
    # ── Python で弾く、JSON パスで手写しする（`is_demo` が落ちていた）、
    # 全件を走査して数える（#219）。提出は SUBMITTED 以降不変なので、
    # 写しが元とずれる余地は構造的に無い。
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
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
    # **一覧に出ている点**（#253）。`score_ratio` は評価そのもので、学習者にも
    # 教員にも見えている点ではない ── 遅延減点を畳んだ後の値がこれである
    # （`aijudge_core.final_score`）。得点の分布を行ではなく集計から出すために
    # 列にした。**書くのは保存時の 1 度きり**で、採点の行は保存前に完成して
    # いるので後から動かない（P8・`worker.py` の `_with_penalty`）。
    #
    # **NULL は「数えない」。** 総合点を保留した採点（#235）と、課題版が
    # 引けない採点がこれに当たる。どちらも一覧に点として出ていない
    # （後者は `load_rows` が行ごと落とす）ので、数から外れるのが正しい。
    final_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    # **この確認を畳んだ後の点**（#253）。教員が段階を直したか、遅延の猶予を
    # 認めたかで、採点の行の `final_ratio` とは別の値になる。
    #
    # **こちらも 1 度きり。** 1 採点に 2 件目の確認は `save_review` が拒む
    # （やり直しは再採点から）ので、書いた後に動かない。確認があれば総合点は
    # 保留されないため、NULL にはならない。
    final_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # **1 採点に確認が複数ありうる**（#275）。以前は `grading_run_id` に
        # UNIQUE を張り、「二度確定できると成績が二つ存在する」としていた。
        #
        # 成績が二つになるのは、**どちらが最終かを決めていない**場合である。
        # 最新を成績とすると決めたので、積み上げてよい ── 誰がいつ何をしたか
        # が全部残り、P8 のとおり過去の確認も書き換えない。
        #
        # TA も 1 件ずつなら確定できる（`require_grader`）ので、そこでの判断
        # ちがいを教員が直せる必要がある。直せる人を教員に限る規則は経路側に
        # ある（`app.py`）── 表は「言われたものを書く」。
        #
        # **一致度（κ）には影響しない。** あちらの標本は blind 採点だけで
        # （`ObservationRecord.usable_for_agreement`）、AI の判定を見ながら
        # 付けた確認は最初から入っていない。
        Index("ix_reviews_run_time", "grading_run_id", "reviewed_at"),
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
    # テナント全体の管理者か（#128）。コースの Enrollment(role=ADMIN) に
    # 頼っていた暫定をここへ移した。既定 False ── 昇格は `aijudge-admin` のみ。
    is_tenant_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Google の `sub`（不変 ID）。ローカル利用者は NULL（#124）。
    # メールアドレスを主キーにしないのは、それが変わりうるため。
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp)

    __table_args__ = (
        # 同じテナント内でログイン ID は一意。別テナントでは衝突してよい
        # （機関をまたいで学籍番号が重なるのは普通のこと）。
        UniqueConstraint("tenant_id", "login", name="uq_users_tenant_login"),
        # 認証方式が違っても同じ Google アカウントの重複作成を防ぐ。
        UniqueConstraint("tenant_id", "external_id", name="uq_users_tenant_external_id"),
    )


class OidcSettingsRow(Base):
    """テナント単位の Google OIDC 設定（#124）。

    1 テナントにつき 1 設定。**このリポジトリは公開物なので、特定機関の
    ドメイン・client_id/secret はここにも他のどこにも書かない** ──
    管理者が `/manage` から設定する値がここに入るだけ。
    """

    __tablename__ = "oidc_settings"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(256))
    # 平文はここに置かない。`Fernet` で暗号化した文字列（persistence 層の
    # 責務。`aijudge_identity` の `OidcSettings` は常に平文を扱う）。
    client_secret_encrypted: Mapped[str] = mapped_column(Text)
    # 1 機関が複数ドメインを許すこともあるので単一値にしない。
    allowed_domains: Mapped[list] = mapped_column(JsonType)
    issuer: Mapped[str] = mapped_column(String(256))
    # ログイン画面のボタンの文言（#209）。**機関ごとの呼び名が入る欄なので、
    # 既定値はモデル側（`aijudge_identity`）が持ち、ここには書かない。**
    login_label: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    updated_at: Mapped[datetime] = mapped_column(Timestamp)


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
    # このコースの共通ルーブリック（観点の宣言）。NULL・空なら組み込みの既定。
    rubric: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    # 共通ルーブリックの畳み方（"or" / "and"）。NULL なら "or"（既定・従来の挙動）。
    rubric_aggregation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # このコースが使う知識要素の正準キー。**空（NULL）なら名前空間の全部。**
    knowledge_components: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    # このコースだけの採点設定の上書き。NULL・空なら雛形のまま。
    grading_overrides: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
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


class CourseGroupRow(Base):
    """コースの中の名簿（`aijudge_core.CourseGroup`）。課題の出題先を絞る。

    名前はコース内で一意 ── API はグループを名前で指す。**幅は模型と揃える**
    （`MAX_GROUP_NAME_LENGTH`）。SQLite は `VARCHAR(n)` の n を守らないので、
    模型で止めないと PostgreSQL でだけ落ちる。
    """

    __tablename__ = "course_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    course_id: Mapped[str] = mapped_column(String(64), ForeignKey("courses.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("course_id", "name", name="uq_course_groups_name"),)


class CourseGroupMemberRow(Base):
    """名簿の 1 行。**置き換えで書く**（`set_group_members`）。"""

    __tablename__ = "course_group_members"

    group_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("course_groups.id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), primary_key=True)

    # 「この人はどのグループか」を引く向き（`groups_of`）。学生画面の一覧と
    # 課題ページのたびに引くので、主キーの逆順に索引を張る。
    __table_args__ = (Index("ix_course_group_members_user", "user_id"),)


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


class CampusNetworkRow(Base):
    """テナント単位の学内アドレス範囲（#333）。

    1 テナントにつき 1 設定（`oidc_settings` と同じ形）。**特定機関の範囲は
    このリポジトリに書かない** ── 管理者が `/manage` から設定する値が入る。
    """

    __tablename__ = "campus_networks"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: CIDR の並び。JSON で持つ ── 行に割ると順序の維持だけのために
    #: 連番が要り、設定の読み書きが 1 件の更新で済まなくなる。
    cidrs: Mapped[dict] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(Timestamp)


class SkillPointRow(Base):
    """習熟度が動いた瞬間の記録（S7・#328）。**追記のみ。**

    `skill_states` は最新の 1 行を持ち替えるので、そこからは推移が読めない。
    学期の途中で「この KC は上がっているのか」を言うにはここが要る ── 根拠は
    最新 20 件に切られ、BKT は観測列を畳むので、後から遡って再現できない。

    **採点結果と同じで書き換えない。** 推定手法は後で差し替えうるが、そのとき
    出した値が何だったかは記録である。
    """

    __tablename__ = "skill_points"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    learner_id: Mapped[str] = mapped_column(String(64))
    kc_id: Mapped[str] = mapped_column(String(64))
    mastery: Mapped[float] = mapped_column(Float)
    observation_count: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(32))
    recorded_at: Mapped[datetime] = mapped_column(Timestamp)

    __table_args__ = (
        # 「このコースの受講者の、この期間の推移」── 分布と推移の画面が使う。
        # 学習者で絞ってから時刻で並べるので、この順序で引く。
        Index("ix_skill_point_tenant_learner_at", "tenant_id", "learner_id", "recorded_at"),
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


class TaskDraftRow(Base):
    """承認待ちの課題（下書き・#321）。**課題表の外に置く。**

    生成物は以前その場で課題として保存し、版を `IN_REVIEW` にして承認を待って
    いた。それだと**承認より前に同一性が決まる** ── 課題キーから課題 ID が
    決まり、提出も採点もそこにぶら下がる。生成物は提案であって確定ではない
    （P5）ので、名前を含めて承認のときに決められる必要がある。

    **版は積まない。** 下書きは承認するまで何度でも直せるもので、履歴を残す
    対象ではない（積むのは課題版だけ・P8）。同じ ID に上書きする。
    """

    __tablename__ = "task_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(64), index=True)
    #: "new" か "revision"。改訂なら `task_id` が入る。
    kind: Mapped[str] = mapped_column(String(16), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
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


class AuditEventRow(Base):
    """監査記録。**誰が**成績に届く何を変えたか（ADR 0016）。

    **運用ログとは別の場所に置く。** 運用ログは journald にあり 90 日で
    消えるが、こちらは学期を跨いで残す ── 成績への異議申立てはその後に来る。
    DB にあることで、`/srv/aijudge` の restic バックアップ（DB ダンプ）に
    自動的に入る。

    **追記のみ。** 更新も削除も経路を作らない。消せる記録は証拠にならない
    （`grading_runs` が追記専用なのと同じ理由、P8）。

    差分は `detail` に JSON で持つ。列にしないのは、行為ごとに意味が違う
    ためで、**検索の条件になるもの（誰・いつ・何を・どの対象）は列にしてある**
    ── そこを JSON に入れると 1 年後に引けない。
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)

    # `user` か `system`。**NULL で「不明」を表さない** ── 締切経過による
    # 自動確定に操作者がいないのは欠落ではなく事実で、後から読む人がその 2 つを
    # 区別できなければ監査にならない。
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 操作時点の役割を焼き込む。`enrollments` を引き直すと、いまの役割で
    # 過去を読むことになる。
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    # **対象が 1 つの id とは限らない。** 受講登録には固有の id が無く、
    # 「どのコースの誰か」の対（`crs_…:usr_…` で 73 字）でしか名指せない。
    # 64 字はこの単純な id ひとつぶんの寸法で、デモコースの自動登録が
    # 最初のログインで 500 を返した（`enrol_into_demo_course`）。
    #
    # **切り詰めて入れる道は取らない** ── 対象を名指せない監査記録は、
    # 後から「誰の何が変わったか」を言えない。自由書式の鍵をここに入れる
    # 必要が出たら、それは `detail` の側の仕事である。
    target_id: Mapped[str] = mapped_column(String(128))

    summary: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict] = mapped_column(JsonType)

    # 運用ログと突き合わせるための鍵（ADR 0016）。
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 個人情報。無くても記録として成立する。
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # 「この提出に何が起きたか」を新しい順に引く経路。
        Index("ix_audit_target", "target_type", "target_id", "at"),
        # 「このテナントの最近の行為」「この行為の履歴」。
        Index("ix_audit_tenant_at", "tenant_id", "at"),
        Index("ix_audit_tenant_action_at", "tenant_id", "action", "at"),
    )


class RunRequestRow(Base):
    """ブラウザ IDE の試しの実行（ADR 0024）。**採点キューとは別の表。**

    `grading_jobs` に相乗りすると、試験中の実行の山が採点を待たせ、採点の山が
    学習者の画面を固める。**結果は残さない** ── 終わった行は runner が
    しばらくで消す（`purge_finished`）。行に学習者のコードが入っているので、
    画面に出したあとまで持つ理由が無い。

    絞り込みと一意性に使うもの（誰の・どの状態の・いつの）は列にし、
    ソース・入力・結果は `document` に入れる（`grading_jobs` と同じ判断）。
    """

    __tablename__ = "run_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    learner_id: Mapped[str] = mapped_column(String(64))
    task_version_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    document: Mapped[dict] = mapped_column(JsonType)

    __table_args__ = (
        # **1 人が同時に待てるのは 1 件**（ADR 0024 §1）。画面でボタンを
        # 押せなくするだけでは境界にならない（#146）── 2 つのタブから同時に
        # 押せば、検査と挿入のあいだをすり抜ける。最後に止めるのは DB である。
        # 部分索引は PostgreSQL・SQLite の両方が持つ。
        Index(
            "uq_run_requests_one_in_flight",
            "learner_id",
            unique=True,
            postgresql_where=text("state IN ('queued', 'running')"),
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        # runner の取得（待っている要求を古い順に）と、期限切れの片付け。
        Index("ix_run_requests_state_created", "state", "created_at"),
        # 連続実行の間隔を見るための「この学習者の最後の要求」。
        Index("ix_run_requests_learner_created", "learner_id", "created_at"),
    )


class IdeBufferRow(Base):
    """IDE の自動保存（設計書 §6.5）。**(学習者, 課題) ごとに 1 行、上書き。**

    提出でも行動記録でもない。採点はここを読まない。鍵を課題版ではなく課題に
    してあるのは、版が上がっても学習者の書きかけを消さないため。

    **DB に置く**（行動記録の本体はファイルに置くのと違う、設計書 §6.6）。
    受付終了時の自動提出が読むもので採点の入口に近く、ストレージの不調で
    提出が止まってはいけない。
    """

    __tablename__ = "ide_buffers"

    learner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    # どの形式で書いているか（`.c`・`.py`・`.md`）。
    suffix: Mapped[str] = mapped_column(String(8))
    # 上限は実行と同じ 64 KiB（`aijudge_ide.MAX_SOURCE_BYTES`）。受け口で断るので
    # 列の幅では縛らない（Text）。
    source: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(Timestamp)


class IdeSubmissionLinkRow(Base):
    """IDE からの提出の出どころ（設計書 §9）。**`submissions` には列を足さない**（I1）。

    行が無い提出はファイルでの提出。本人が押した提出（`editor`）と、受付終了時に
    サーバが出した提出（`auto_close`）を後から区別するために残す。
    """

    __tablename__ = "ide_submission_links"

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    learner_id: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    origin: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    ide_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(Timestamp)
