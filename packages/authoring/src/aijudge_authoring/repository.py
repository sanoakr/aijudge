"""課題の保存先（S2）。

課題は S2 の持ち物なので、保存先の契約もここに置く。採点側（S5）と
提出側（S3）は課題を作らず読むだけで、両者が別々に取り込むと
「表示している観点」と「採点した観点」が食い違いうる。

`TaskVersion` は公開後不変（P8）。保存済みの版を書き換えようとしたら
拒否する。問題文の訂正は新しい版を作る。

**「同じ内容」の判定から `created_at` を外す。** 不変にしたいのは採点の
基準（問題文・観点・テストケース）であって、行を書いた時刻ではない。
含めると、同じディレクトリの再取り込みが毎回「内容が違う」と拒否され、
学期の頭に取り込みを流し直せなくなる。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from aijudge_core import ReviewState, Task, TaskVersion
from aijudge_core.ids import CourseId, TaskId, TaskVersionId, UserId

from .draft_store import TaskDraftRecord
from .verification import TaskChecks


class TaskStoreError(Exception):
    """課題の保存先の不整合。"""


class TaskImmutabilityViolation(TaskStoreError):
    """公開済みの TaskVersion を書き換えようとした（P8）。"""


@runtime_checkable
class TaskRepository(Protocol):
    """課題と課題版の保存先。"""

    def save_task(self, task: Task) -> None: ...

    def get_task(self, task_id: TaskId) -> Task | None: ...

    def save_version(self, version: TaskVersion) -> None:
        """課題版を保存する。同じ ID の版が既にあれば拒否する（P8）。"""
        ...

    def get_version(self, version_id: TaskVersionId) -> TaskVersion | None: ...

    def get_versions(
        self, version_ids: Iterable[TaskVersionId]
    ) -> dict[TaskVersionId, TaskVersion]:
        """複数の版を **1 回で**引く。無い ID は結果に含めない。

        学習者の画面が、自分の提出が指す版（いまの版とは限らない）を引くために
        ある ── 版が上がった後も前の版への提出を見せるには、その提出の課題と
        観点を知る必要がある。提出ごとに `get_version` すると一覧を開くたびに
        提出数ぶんの問い合わせになる。
        """
        ...

    def versions_for_tasks(self, task_ids: Iterable[TaskId]) -> tuple[TaskVersion, ...]:
        """複数の課題の版を**全部、1 回で**引く（順不同）。

        配点（`effective_max_score`）は版の履歴から決まるので、一覧で課題ごとに
        `list_versions` すると課題数ぶんの問い合わせになる。
        """
        ...

    def latest_version(self, task_id: TaskId) -> TaskVersion | None: ...

    def latest_published_version(self, task_id: TaskId) -> TaskVersion | None:
        """**学習者に出してよい**最新版。承認済みが 1 つも無ければ None。"""
        ...

    # -- 承認待ちの下書き（#321）。**課題表の外に置く** ------------------
    #
    # 生成物は承認するまで課題にしない（`draft_store` の冒頭）。契約をここに
    # 並べるのは、保管の口が 1 つであるため ── 別の Protocol に分けると、
    # 合成ルートが 2 つの保管を持ち回ることになる。

    def save_draft(self, draft: TaskDraftRecord) -> None:
        """下書きを保存する。**同じ ID なら上書き**（版は積まない）。"""
        ...

    def get_draft(self, draft_id: str) -> TaskDraftRecord | None: ...

    def list_drafts(self, course_id: CourseId) -> tuple[TaskDraftRecord, ...]:
        """このコースの承認待ち。**古い順。**"""
        ...

    def delete_draft(self, draft_id: str) -> None:
        """下書きを消す。**却下も削除もこれ**（ADR 0019）。無い ID でも落とさない。"""
        ...

    def list_versions(self, task_id: TaskId) -> tuple[TaskVersion, ...]:
        """この課題の版を、**新しい順**に全部（#319）。

        版は上書きせず積む（P8）ので、履歴はそのまま残っている ── 画面から
        読めなかっただけである。戻したい版を選ぶには、何があるかが見えて
        いなければならない。
        """
        ...

    def list_for_course(self, course_id: CourseId) -> tuple[Task, ...]:
        """コースの課題一覧。学生 UI と教員 UI が使う。"""
        ...

    def list_versions_in_review(self) -> tuple[TaskVersion, ...]:
        """教員のレビュー待ちの課題版。**生成物が溜まる場所。**"""
        ...

    def record_review(
        self, version_id: TaskVersionId, *, approved: bool, reviewer: UserId, reason: str | None
    ) -> TaskVersion:
        """レビューの結果を書き戻す。

        `save_version` と分けてあるのは、**動いてよい項目が違う**からである
        （`REVIEW_FIELDS`）。同じ口にすると、レビューのつもりで問題文を
        差し替えられる ── 出題済みの課題が黙って変わる。
        """
        ...

    def save_checks(self, version_id: TaskVersionId, checks: TaskChecks) -> None:
        """課題版に対して走らせた検査の結果を残す。

        **上書きしてよい。** 課題版と違って検査は何度でも走らせられる
        （門を直したら測り直す）。残すのは最後に走らせた結果である。
        """
        ...

    def get_checks(self, version_id: TaskVersionId) -> TaskChecks | None: ...


# 不変性の比較から外す項目。採点の基準ではないもの。
#
# `points_declared` は「この版の `max_score` を配点として書いたか」という
# 目印（2026-09-25）。配点そのもの（`max_score`）は比較に残る。目印まで比べると、
# 目印の無い既存の版を同じ内容で入れ直すたびに内容が違うことになる。
VOLATILE_FIELDS = frozenset({"created_at", "points_declared"})

# **同一性**の項目。`content` が外し、`substantive` が残すもの。
#
# ID は課題キーと版番号から導かれる（`build_task_version`）ので、**内容が
# 同じでも版が違えば違う値になる。**「この版とあの版は同じ内容か」を訊く
# 側がここを見ると、答えは常に「違う」になる。
IDENTITY_FIELDS = frozenset({"id", "version"})

# 出所のうち、レビューで動いてよい項目。
#
# **動いてよいのはここだけである。** `authored_by` / `generated_by` /
# `generation_prompt_version` は「この課題がどこから来たか」という事実で、
# 後から書き換われば承認率の測定が意味を失う（誰が書いたことにもできる）。
# 一方 `review_state` は状態機械そのもので、動かなければレビューが成立しない。
REVIEW_FIELDS = frozenset({"review_state", "reviewed_by", "reject_reason"})


def substantive(version: TaskVersion) -> dict:
    """採点の基準になる部分だけを取り出す。

    `created_at` を含めないのがこの関数の存在理由（モジュール docstring 参照）。

    **レビューの状態も外す。** 不変にしたいのは採点の基準（問題文・観点・
    テストケース）であって、教員がまだ読んでいないという事実ではない。
    含めると、課題を承認した瞬間に「内容が違う」と拒否される。
    """
    dumped = version.model_dump(mode="json", exclude=set(VOLATILE_FIELDS))
    provenance = dumped.get("provenance")
    if isinstance(provenance, dict):
        dumped["provenance"] = {
            key: value for key, value in provenance.items() if key not in REVIEW_FIELDS
        }
    return dumped


def content(version: TaskVersion) -> dict:
    """**同じ内容か**を訊くための取り出し。`substantive` から同一性を外す。

    区別が要るのは、この 2 つが別のことを訊いているからである。

    `substantive`  この ID の版は、保存済みのものと同じか（**不変性**）。
                   比較の両辺は同じ ID を指しているので、ID も版番号も等しい。
                   `save_version` が使う。
    `content`      いまの版と、これから作ろうとしている版は同じ内容か
                   （**訂正するに値するか**）。両辺の ID は必ず違う ──
                   ID は課題キーと版番号から導かれるので、版 2 の隣に置く
                   候補は版 1 として組まれ、ID が一致しない。

    区別が無かったあいだ、`substantive` が両方に使われていた。後者では
    `id` と `version` が必ず食い違うので、**比較は常に「内容が違う」を
    返していた** ── 実測（2026-09-22）で、同じ内容の訂正を 3 回流すと版が
    1 → 2 → 3 と増えた。`course apply --revise` を流し直すたびに全課題の
    版が 1 つ増え、教員コンソールの「この問題を保存して更新する」も、
    何も直さずに押しただけで版を作っていた。束の取り込み前に変化を数える
    `plan_bundle` も、版 2 以上の課題を全部「訂正される」と数えていた。

    **出所（`provenance.authored_by`）は残す。** 誰が書いたかは内容の一部
    ではないが、比較の両辺で作者を揃えずに数えると全件が「変わった」に
    見えるため、呼び出し側には保存で使う値をそのまま渡させる
    （`plan_bundle` の docstring）。ここで外すと、その約束が緩む。

    **`q_matrix` の中の `task_version_id` も外す。** ここに `id` と同じ
    素材（`task_version_id`）が埋め込まれており、`exclude=IDENTITY_FIELDS`
    はトップレベルしか外さないので素通りしていた ── 知識要素を 1 つでも
    宣言した課題は、`q_matrix` が非空になった時点でこの関数の存在理由その
    ものが壊れる。実測（2026-09-22）で、`network` コースの知識要素つき課題
    9 件中 7 件が、`statement` も `criteria` も一字一句同じなのに
    `q_matrix[].task_version_id` だけで「内容が違う」と判定された。KC は
    この基盤の中核機能なので、実運用の課題のほとんどがここを通る。
    `q_matrix` の中身のうち内容なのは `kc_id` / `weight` / `required` で、
    `task_version_id` は版番号から機械的に導かれる外部キーでしかない。
    """
    dumped = {
        key: value for key, value in substantive(version).items() if key not in IDENTITY_FIELDS
    }
    q_matrix = dumped.get("q_matrix")
    if isinstance(q_matrix, list):
        dumped["q_matrix"] = [
            {key: value for key, value in entry.items() if key != "task_version_id"}
            if isinstance(entry, dict)
            else entry
            for entry in q_matrix
        ]
    return dumped


class InMemoryTaskRepository:
    """テストと開発用。規則は本番と同じにしてある。"""

    def __init__(self) -> None:
        self._tasks: dict[TaskId, Task] = {}
        self._versions: dict[TaskVersionId, TaskVersion] = {}
        self._order: list[TaskVersionId] = []
        self._checks: dict[TaskVersionId, TaskChecks] = {}
        # 承認待ちの下書き（#321）。**課題とは別に持つ** ── 承認するまで課題は
        # 存在しない（`draft_store` の冒頭）。
        self._drafts: dict[str, TaskDraftRecord] = {}
        self._draft_order: list[str] = []

    # -- 下書き（#321）------------------------------------------------------

    def save_draft(self, draft: TaskDraftRecord) -> None:
        if draft.id not in self._drafts:
            self._draft_order.append(draft.id)
        self._drafts[draft.id] = draft

    def get_draft(self, draft_id: str) -> TaskDraftRecord | None:
        return self._drafts.get(draft_id)

    def list_drafts(self, course_id: CourseId) -> tuple[TaskDraftRecord, ...]:
        return tuple(
            self._drafts[key]
            for key in self._draft_order
            if key in self._drafts and self._drafts[key].course_id == course_id
        )

    def delete_draft(self, draft_id: str) -> None:
        # **無い ID でも落とさない。** 二度押しは普通に起きる（`DraftStore`）。
        self._drafts.pop(draft_id, None)
        if draft_id in self._draft_order:
            self._draft_order.remove(draft_id)

    def save_task(self, task: Task) -> None:
        self._tasks[task.id] = task

    def get_task(self, task_id: TaskId) -> Task | None:
        return self._tasks.get(task_id)

    def save_version(self, version: TaskVersion) -> None:
        existing = self._versions.get(version.id)
        if existing is not None:
            if substantive(existing) == substantive(version):
                # 同じ内容の取り込みを繰り返すのは冪等な操作。
                # 決定的 ID（derived_id）で取り込む経路では普通に起きる。
                # 保存済みの側を残す（取り込み時刻を書き換えない）。
                return
            raise TaskImmutabilityViolation(
                f"TaskVersion {version.id} already exists with different content; "
                "corrections create a new version (P8)"
            )
        self._versions[version.id] = version
        self._order.append(version.id)

    def get_version(self, version_id: TaskVersionId) -> TaskVersion | None:
        return self._versions.get(version_id)

    def get_versions(
        self, version_ids: Iterable[TaskVersionId]
    ) -> dict[TaskVersionId, TaskVersion]:
        return {vid: self._versions[vid] for vid in version_ids if vid in self._versions}

    def versions_for_tasks(self, task_ids: Iterable[TaskId]) -> tuple[TaskVersion, ...]:
        wanted = set(task_ids)
        return tuple(v for v in self._versions.values() if v.task_id in wanted)

    def latest_version(self, task_id: TaskId) -> TaskVersion | None:
        versions = [
            self._versions[vid] for vid in self._order if self._versions[vid].task_id == task_id
        ]
        if not versions:
            return None
        return max(versions, key=lambda v: v.version)

    def list_versions(self, task_id: TaskId) -> tuple[TaskVersion, ...]:
        versions = [
            self._versions[vid] for vid in self._order if self._versions[vid].task_id == task_id
        ]
        return tuple(sorted(versions, key=lambda v: v.version, reverse=True))

    def latest_published_version(self, task_id: TaskId) -> TaskVersion | None:
        versions = [
            self._versions[vid]
            for vid in self._order
            if self._versions[vid].task_id == task_id and self._versions[vid].is_published
        ]
        if not versions:
            return None
        return max(versions, key=lambda v: v.version)

    def save_checks(self, version_id: TaskVersionId, checks: TaskChecks) -> None:
        self._checks[version_id] = checks

    def get_checks(self, version_id: TaskVersionId) -> TaskChecks | None:
        return self._checks.get(version_id)

    def list_versions_in_review(self) -> tuple[TaskVersion, ...]:
        return tuple(
            self._versions[vid]
            for vid in self._order
            if self._versions[vid].provenance.review_state is ReviewState.IN_REVIEW
        )

    def record_review(
        self, version_id: TaskVersionId, *, approved: bool, reviewer: UserId, reason: str | None
    ) -> TaskVersion:
        version = self._versions.get(version_id)
        if version is None:
            raise TaskStoreError(f"課題版が見つかりません: {version_id}")
        updated = version.model_copy(
            update={
                "provenance": version.provenance.reviewed(
                    approved=approved, reviewer=reviewer, reason=reason
                )
            }
        )
        self._versions[version_id] = updated
        return updated

    def list_for_course(self, course_id: CourseId) -> tuple[Task, ...]:
        return tuple(
            sorted(
                (task for task in self._tasks.values() if task.course_id == course_id),
                key=lambda task: task.id,
            )
        )
