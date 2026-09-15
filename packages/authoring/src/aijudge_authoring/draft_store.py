"""承認待ちの課題（下書き）の型と保管の契約（#321）。

## なぜ課題表の外に置くか

生成した課題は、以前は**その場で課題（`Task`）として保存し**、版を
`IN_REVIEW` にして承認を待っていた。それだと**承認より前に同一性が決まる**。

    課題キー → 課題 ID（`derived_id`）→ 提出も採点もこの ID にぶら下がる

キーは変えられない（変えれば別の課題になる）ので、**生成した時点で
「この課題はこういう名前である」と決め切る**ことになる。しかし生成物は
提案であって確定ではない（P5）── 中身を読んでから名前を直したい、というのが
承認という段の意味そのものである。

課題表の外に置けば、**承認するまで課題は存在しない**。下書きは制約なく
直せるし、消せる。承認したときに初めて課題（または新しい版）になる。

## 2 種類ある

    新規   まだ課題が無い。承認すると課題が作られる（キーはここで確定）。
    改訂   既にある課題の書き直し（#306）。承認すると新しい版になる。
           **キーは変えられない** ── 同じ課題の改訂であって別の課題ではない。

## 却下は残さない

却下した下書きは消す（2026-09-15 決定・ADR 0019）。承認率を測るための記録は
残らない ── その数は「生成の質」ではなく「いまのプロンプト × この教員の好み」
を測っており、単一の合格基準にする意味が無いと判断した。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import CourseId, TaskId, UserId

from .spec import TaskSpec
from .verification import TaskChecks

__all__ = ["DraftId", "DraftKind", "DraftStore", "TaskDraftRecord"]

#: 下書きの ID。課題 ID とは別の空間（承認するまで課題は無い）。
DraftId = str


class DraftKind(StrEnum):
    """新規の課題か、既にある課題の改訂か。"""

    NEW = "new"
    REVISION = "revision"


class TaskDraftRecord(BaseModel):
    """承認待ちの課題 1 件。

    **中身は `TaskSpec`。** 承認したときに通す型と同じものを持つ ── 別の型に
    しておくと、承認の瞬間に詰め替えが要り、そこで落ちるものが出る
    （`draft_to_spec` が生成物を手書きの課題と同じ型に落としているのと同じ
    判断・P1）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: DraftId
    course_id: CourseId
    kind: DraftKind
    spec: TaskSpec
    #: 改訂のときだけ。どの課題の書き直しか。
    task_id: TaskId | None = None
    #: 出題する問題セット（新規のときに選ぶ。承認の瞬間まで決めなくてよい）。
    unit: str = ""
    #: 何を直したと言っているか（改訂・#306）。差分を読む手掛かり。
    changes: tuple[str, ...] = ()
    #: 出所（P8）。どのモデルのどのプロンプト版が書いたか。
    generated_by: str = ""
    generation_prompt_version: str = ""
    created_by: UserId | None = None
    created_at: datetime
    #: 検査の結果（あれば）。**判定ではなく判断材料**（ADR 0008）。
    #: 課題版に紐づける表（`task_checks`）は課題ができてからしか使えないので、
    #: 下書きのあいだはここが持つ。採用したときに課題版へ移す。
    checks: TaskChecks | None = None
    #: 採点に使う科目プロファイル。課題が自分で持つ（ADR 0018）。
    subject_profile: str = ""
    #: 提出できる拡張子（課題の指定。空ならコースの既定）。
    accepted_suffixes: tuple[str, ...] = ()
    #: 読みやすさの重み（観点を宣言しない課題で使う）。
    readability_weight: float = Field(default=0.0, ge=0.0, lt=1.0)


class DraftStore(Protocol):
    """下書きの保管。**課題表とは別**（この冒頭の理由）。"""

    def save_draft(self, draft: TaskDraftRecord) -> None:
        """下書きを保存する。**同じ ID なら上書きする** ── 承認までは何度でも
        直せるのが下書きであり、版を積む対象ではない（積むのは課題版だけ・P8）。
        """
        ...

    def get_draft(self, draft_id: DraftId) -> TaskDraftRecord | None: ...

    def list_drafts(self, course_id: CourseId) -> tuple[TaskDraftRecord, ...]:
        """このコースの承認待ち。**古い順** ── 溜まった順に片付ける。"""
        ...

    def delete_draft(self, draft_id: DraftId) -> None:
        """下書きを消す。**却下も削除もこれ**（ADR 0019）。

        **存在しない ID でも落とさない。** 二度押しは普通に起きる（承認の
        あと戻って却下する、など）し、結果は同じ「もう無い」である。
        """
        ...
