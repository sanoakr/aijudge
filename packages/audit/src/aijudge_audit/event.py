"""監査記録の型。

**運用ログとは別物**（ADR 0016）。運用ログは journald に出てローテートで
消える best-effort の層で、こちらは「誰が成績に届く何を変えたか」を
DB に append-only で残す、**消えては困る**層である。書けなければ操作ごと
失敗させる ── 成績の変更が記録なしで成立してはいけない。

**`HumanReview` とも別物。** 監査行は κ の証拠ではない。教員が 1 件を読めば
`HumanReview` が 1 件と監査行が 1 本できるが、意味は重ならない
（`HumanReview` は「この提出を読んだ」、監査行は「その操作が行われた」）。
2 つを畳んで一致度を壊した前例が ADR 0010 にある。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aijudge_core.ids import TenantId, UserId


class ActorKind(StrEnum):
    """誰がやったか。**3 つある。**

    `None` で「不明」を表さない。締切経過による自動確定に人間の操作者が
    いないのは記録の欠落ではなく事実で、後から読む人がその 2 つを区別
    できなければ監査にならない（ADR 0010 が `Finalization` と `HumanReview` を
    分けたのと同じ理由）。

    - `USER` — 認証済みの誰かが行った。`actor_user_id` が要る。
    - `SYSTEM` — 人間の操作者がいない（締切経過による自動確定など）。
    - `ANONYMOUS` — **認証されていない誰かが試みた。** ログイン失敗がこれ。

    `ANONYMOUS` を `USER` に畳まないのが要点である。ログイン失敗は
    「その利用者が行ったこと」ではない ── パスワードを間違えたのが本人とは
    限らず、むしろ本人でない場合こそ記録が要る。**対象**は口座（`target_id`）、
    **操作者**は不明、と分けて書く。
    """

    USER = "user"
    SYSTEM = "system"
    ANONYMOUS = "anonymous"


class AuditAction(StrEnum):
    """記録する行為。

    列挙にしてあるのは、後から `WHERE action = ...` で引くため。文字列自由だと
    表記ゆれが積もって、1 年後に「この操作を誰がやったか」を引けなくなる。
    """

    # -- 認証 --
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    LOGGED_OUT = "logout"
    # -- 資格情報 --
    USER_CREATED = "user.created"
    USER_DISABLED = "user.disabled"
    PASSWORD_CHANGED = "password.changed"
    PASSWORD_REISSUED = "password.reissued"
    TOKEN_ISSUED = "token.issued"
    TOKEN_REVOKED = "token.revoked"
    # -- 権限 --
    ENROLLED = "enrolment.changed"
    TENANT_ADMIN_CHANGED = "tenant_admin.changed"
    # -- 成績 --
    REVIEW_RECORDED = "review.recorded"
    GRADE_FINALIZED = "grade.finalized"
    # -- 採点の設定 --
    COURSE_UPDATED = "course.updated"
    TASK_UPDATED = "task.updated"
    PROFILE_UPDATED = "profile.updated"
    PROFILE_DUPLICATED = "profile.duplicated"


# `detail` に入れてよい大きさの上限。差分の前後の値を持つための欄であって、
# 提出物の本文を置く場所ではない（P7）。監査ログは学習者データの保管庫ではない。
MAX_DETAIL_CHARS = 4000


class AuditEvent(BaseModel):
    """1 つの行為の記録。**作ったら変えない。**

    追記専用なのは採点結果と同じ理由による（P8）。後から書き換えられる記録は
    「誰が何をしたか」の証拠にならない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    at: datetime
    tenant_id: TenantId

    actor_kind: ActorKind
    actor_user_id: UserId | None = None
    # 操作時点の役割。あとで役割が変わっても、そのときの権限で読めるように
    # 焼き込む（`enrollments` を引き直すと、いまの役割で過去を見ることになる）。
    actor_role: str | None = None

    action: AuditAction
    # 何に対して。`("submission", "sub_...")` のような組。型を持たせるのは、
    # id だけでは何のことか後から分からないため。
    target_type: str = Field(min_length=1)
    # **列と同じ上限を模型にも持たせる**（`summary` と同じ理由）。持たせないと
    # 桁あふれは COMMIT のときの `DataError` になり、**同じ unit_of_work に
    # いる呼び出し側の書き込みごと巻き戻る** ── 受講登録の対が 64 字の列に
    # 入らず、ログインが丸ごと 500 になったのが実例である。ここで弾けば、
    # メモリ実装のテストでも同じ失敗が見える。
    target_id: str = Field(min_length=1, max_length=128)

    # 人が読む 1 行。画面に出す前提で書く。
    summary: str = Field(min_length=1, max_length=500)
    # 機械が読む差分。**前後の値を入れ、本文は入れない。**
    detail: dict[str, Any] = Field(default_factory=dict)

    # 運用ログと突き合わせるための鍵（ADR 0016）。
    request_id: str | None = None
    # 個人情報なので保存期間の対象。無くても記録として成立する。
    source_ip: str | None = None

    @model_validator(mode="after")
    def _check_actor(self) -> Self:
        if self.actor_kind is ActorKind.USER and self.actor_user_id is None:
            raise ValueError("a user action must name the user who performed it")
        if self.actor_kind is not ActorKind.USER and self.actor_user_id is not None:
            # SYSTEM にも ANONYMOUS にも操作者はいない。`target_id` に口座を
            # 書くのは構わないが、**やった人として名前を付けない。**
            raise ValueError(
                "only a user action names an actor; do not attribute an unattributed one"
            )
        return self

    @model_validator(mode="after")
    def _check_detail_is_not_a_dumping_ground(self) -> Self:
        """`detail` に提出物を置かせない（P7）。

        監査ログは DB にあり、バックアップにも入り、消さない。**そこへ
        学習者データを写すと、消えない場所に増える。** 差分の前後の値なら
        この上限に収まる。
        """
        if len(repr(self.detail)) > MAX_DETAIL_CHARS:
            raise ValueError(
                f"detail は {MAX_DETAIL_CHARS} 文字以内にすること"
                "（差分を入れる欄であって、提出物の本文を置く場所ではない）"
            )
        return self
