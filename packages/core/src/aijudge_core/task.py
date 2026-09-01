"""課題（Task）とルーブリック。

TaskVersion を不変にしているのは採点の再現性（P8）のため。
問題文やルーブリックが直った後に過去の採点を読み返しても、
そのとき何を基準に採点したのかが必ず分かる。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import CourseId, CriterionId, TaskId, TaskVersionId, UserId
from .knowledge import QMatrixEntry


class ReviewState(StrEnum):
    """教員レビューの状態。AI 生成問題も手動作成問題も同じ状態機械を通る。"""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Provenance(BaseModel):
    """この TaskVersion が誰の・何の産物か。

    AI 生成であることを隠さない。採点側はこれを見ないが（P1）、
    教員向けの表示と、生成品質の統計（PoC-2 の承認率）に必要。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authored_by: UserId | None = None
    generated_by: str | None = None
    generation_prompt_version: str | None = None
    review_state: ReviewState = ReviewState.DRAFT
    reviewed_by: UserId | None = None
    reject_reason: str | None = None

    def reviewed(
        self, *, approved: bool, reviewer: UserId, reason: str | None = None
    ) -> Provenance:
        """教員が読んだ結果を載せた新しい出所を返す。

        **却下理由は捨てない**（設計方針 §5）。生成の品質を上げる材料は
        「何が却下されたか」ではなく「なぜ却下されたか」の方にある。
        Phase 4 の合格基準（教員承認率 ≥ 60%）も、分母に却下が要る。

        **二度目のレビューを拒む。** 承認済みの課題を後から却下できると、
        既に出題した課題が「承認されていない」ことになりうる。やり直しは
        新しい版を作る（P8）。
        """
        if self.review_state in (ReviewState.APPROVED, ReviewState.REJECTED):
            raise ValueError(
                f"this task version is already {self.review_state.value}; "
                "a change of mind creates a new version (P8)"
            )
        if not approved and not (reason and reason.strip()):
            raise ValueError("却下には理由が要ります")
        return self.model_copy(
            update={
                "review_state": ReviewState.APPROVED if approved else ReviewState.REJECTED,
                "reviewed_by": reviewer,
                "reject_reason": None if approved else reason,
            }
        )

    @model_validator(mode="after")
    def _check_origin(self) -> Self:
        if self.authored_by is None and self.generated_by is None:
            raise ValueError("Provenance requires either authored_by or generated_by")
        if self.review_state is ReviewState.REJECTED and not self.reject_reason:
            # 却下理由は作問モデル改善の学習データになるので必須にする。
            raise ValueError("a rejected TaskVersion must carry a reject_reason")
        return self


class RubricLevel(BaseModel):
    """観点内の到達段階。`score_ratio` はその観点の配点に対する比率。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: int = Field(ge=0)
    label: str = Field(min_length=1)
    descriptor: str = Field(min_length=1)
    score_ratio: float = Field(ge=0.0, le=1.0)


class Aggregation(StrEnum):
    """観点どうしをどう畳むか。**ルーブリック単位の設定である。**

    OR   評価順の上から最後まで独立に評価し、重み付き和を採る。**既定**で、
         これまでの唯一の挙動。観点は互いに影響しない。
    AND  評価順の上から評価し、**いずれかの観点が 0% になったらそこで止める。**
         以降の観点は評価せず、0% として重みどおり数える。「動かないコードの
         読みやすさを評価しても意味が無い」という順序関係を表すためにある。

    **AND で打ち切った観点は「採点できなかった」ではない。** 0% は確定した
    結果で、保留ではない。混ぜると重みが再配分されて**打ち切られた学習者の点が
    上がる**（ADR 0015）ので、採点結果では `skipped_criteria` に分けて入る。

    観点ごとの AND/OR ではない。ある観点だけを AND にすると「何より上なら
    止まるのか」が観点の並びからは読めなくなる。
    """

    OR = "or"
    AND = "and"


def effective_aggregation(
    task_value: Aggregation | None, course_value: Aggregation | None
) -> Aggregation:
    """実際に効く集約方式。**課題の指定が科目の既定を上書きする。**

    課題に何も入れていなければコースの値。コースにも無ければ OR
    （＝これまでの挙動。宣言するまで何も変わらない）。猶予
    （`grace_minutes`）と同じ形にしてあるのは、同じ性質の設定だから ──
    成績に直接効き、教員が学期中に決め、課題ごとに例外がありうる。
    """
    if task_value is not None:
        return task_value
    return course_value or Aggregation.OR


# 「この観点は機械に採点させない（人が採点する）」を表す評価器の名前。
#
# **空（None）とは別の状態である。** 空は「どの AI 評価器からも対象」を
# 意味しており（`pipeline` の `criterion.evaluator_id not in (None, evaluator_id)`）、
# 画像の判定のようにまだ機械が持っていない観点をそこへ入れると、AI が
# 見当違いの判定を返す。名前を持たせて、**誰にも渡さない**ことを宣言する。
#
# 名前にしたのは、宣言（`CriterionSpec`）から模型（`RubricCriterion`）、
# 画面の `<select>` までを同じ 1 つの値で通せるため。読み落とした場所は
# 「知らない評価器の名前」として扱われ、その観点は誰にも採点されずに
# レビューへ回る ── 静かに間違った点が出るのではなく、止まる側に倒れる。
HUMAN_SCORED = "__human__"


class RubricCriterion(BaseModel):
    """ルーブリックの 1 観点。AI 評価器はこの単位で 1 回呼ばれる（§04）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CriterionId
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: float = Field(gt=0.0, le=1.0)
    levels: tuple[RubricLevel, ...] = Field(min_length=2)
    evaluator_id: str | None = None

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        levels = [level.level for level in self.levels]
        if len(set(levels)) != len(levels):
            raise ValueError("RubricLevel.level must be unique within a criterion")
        if sorted(levels) != levels:
            raise ValueError("RubricLevel entries must be ordered by level")
        if max(level.score_ratio for level in self.levels) != 1.0:
            raise ValueError("the top RubricLevel must have score_ratio 1.0")
        return self

    @property
    def scored_by_human(self) -> bool:
        """機械に採点させない観点か（人が採点する）。

        真なら決定的評価器にも AI 評価器にも渡さない。採点結果では
        `GradingRun.awaiting_human` に入り、人が段階を入れるまで
        総合点は出ない（ADR 0015）。
        """
        return self.evaluator_id == HUMAN_SCORED

    def level_for(self, level: int) -> RubricLevel:
        for candidate in self.levels:
            if candidate.level == level:
                return candidate
        raise KeyError(f"no level {level} in criterion {self.code!r}")


class TestCase(BaseModel):
    """決定的評価器が使う検証データ。中身の解釈は Evaluator に任せる（P1）。

    コードのテストケースも、数式の同値判定に使う参照式も、
    レポートの必須節リストも、すべてこの型に載せる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    evaluator_id: str = Field(min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)
    hidden: bool = True
    weight: float = Field(default=1.0, gt=0.0)


class TaskVersion(BaseModel):
    """課題の 1 版。公開後は不変。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TaskVersionId
    task_id: TaskId
    version: int = Field(ge=1)
    subject_profile: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    reference_solution: str | None = None
    # **並びが評価順である。** AND のときは上から評価して 0% で打ち切る。
    criteria: tuple[RubricCriterion, ...] = Field(min_length=1)
    # 観点の畳み方。None ならコースの設定に従う（`effective_aggregation`）。
    aggregation: Aggregation | None = None
    test_cases: tuple[TestCase, ...] = ()
    q_matrix: tuple[QMatrixEntry, ...] = ()
    max_score: float = Field(gt=0.0)
    allow_handwriting: bool = False
    # この版を作った `TaskSpec.key`。**訂正のときに要る** ── ID は鍵から
    # 導いてあり（`derived_id`）、鍵が無いと次の版の ID も観点の ID も
    # 作れない。古い版には入っていないので None を許す。
    source_key: str | None = None
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def _check_weights(self) -> Self:
        total = sum(criterion.weight for criterion in self.criteria)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"rubric weights must sum to 1.0, got {total}")
        codes = [criterion.code for criterion in self.criteria]
        if len(set(codes)) != len(codes):
            raise ValueError("RubricCriterion.code must be unique within a TaskVersion")
        for entry in self.q_matrix:
            if entry.task_version_id != self.id:
                raise ValueError("QMatrixEntry.task_version_id must match the TaskVersion")
        return self

    @property
    def is_published(self) -> bool:
        return self.provenance.review_state is ReviewState.APPROVED

    def criterion(self, criterion_id: CriterionId) -> RubricCriterion:
        for criterion in self.criteria:
            if criterion.id == criterion_id:
                return criterion
        raise KeyError(f"no criterion {criterion_id!r} in {self.id!r}")


class SubmissionWindow(StrEnum):
    """いま提出できるか、できるとしてどの扱いか（#73）。

    **3 つに分かれるのは締切で閉じないから。** 締切は「ここから減点が始まる」
    で、閉じるのは受付終了である（ADR 0013）。学習者の画面はこの区分で
    並べ替えられ、教員が置いた 2 つの時刻がそのまま見える形になる。
    """

    # 提出開始前。学習者の画面には出ない。
    NOT_OPEN = "not_open"
    # 締切前。減点なしで出せる。
    OPEN = "open"
    # 締切後・受付終了前。**出せるが減点される。**
    LATE = "late"
    # 受付終了後。出せない。
    CLOSED = "closed"


class Task(BaseModel):
    """課題そのもの。版をまたいだ同一性を担う。

    `unit` と `session` は課題を「何回目の課題か」でまとめるためにある。
    1 回の授業で複数問（`p1 p2 p3`）出るので、一覧を平らに並べると
    学習者も教員も何回目の分を見ているのか分からなくなる。

    `unit` は問題セットの名前（`ex03` など）で、同一性の鍵。`session` は
    並べ替え用の数値。`unit` から機械的に取れないことがある（`exam08` の
    ような名前）ので別に持つ。

    **日程は問題セットで揃える。** 公開・提出開始・締切・自動確定の猶予は
    課題ごとに持つが、値を決めるのは問題セット単位である（画面がそう作って
    ある）。課題ごとに持つのは、課題が課題セットへの参照ではなく `unit` と
    いう名前でしか結びついていないため ── セットの実体を別に作ると、
    取り込みのたびに 2 つの記録を揃えなければならなくなる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TaskId
    course_id: CourseId
    title: str = Field(min_length=1)
    # 何回目のまとまりか（例: "ex03"）。取り込み元のディレクトリ名。
    unit: str | None = None
    # 何回目か。並べ替えに使う。`unit` から取れないこともある。
    #
    # **0 を許す。** ガイダンス回・事前課題を「第 0 回」と呼ぶ運用が実在し、
    # `None` で代用すると `sort_key` が末尾へ送るので、番号を持たせた意味が
    # 0 回だけ失われる（#60）。1 始まりだったのは取り込み元の `exNN` に
    # 0 回が無かったからで、画面から回を作れるようになって前提が変わった。
    session: int | None = Field(default=None, ge=0)
    # まとまりの中での順序（p1, p2, … の 1, 2, …）。
    position: int | None = Field(default=None, ge=1)
    current_version_id: TaskVersionId | None = None
    # 公開日時。学習者に見せる「何日提示の課題か」がこれ。
    opens_at: datetime | None = None
    # 提出を受け付け始める時刻。**空なら公開と同時に受け付ける。**
    # 公開と分けるのは、課題文を先に配って提出は演習時間に開ける運用が
    # あるため。ここが未来なら提出は受け付けない（学習者側で拒否する）。
    submissions_open_at: datetime | None = None
    due_at: datetime | None = None
    # 採点を始める時刻。**空なら提出と同時に採点する**（従来どおり）。
    #
    # 試験のための値である（#67）。テスト実行の結果は「どのケースで落ちたか」を
    # 含むので、**試験中の学習者にとっては答えの一部**になる。返し続けると
    # 提出 → 結果 → 直して再提出が回り、実力ではなく試行回数を測ることになる。
    #
    # **採点しない**のであって、結果を隠すのではない。隠す作りは「見せない
    # 判断」が 1 か所抜けた時点で答えが漏れるが、こちらは採点結果がまだ
    # 存在しないので漏れる経路そのものが無い。
    #
    # 締切とは別に持つ。試験の終了と採点の開始はふつう同じ時刻だが、
    # 同じ値にすると延長のたびに採点開始も動いてしまう。
    grading_starts_at: datetime | None = None
    # 提出の受付を終える時刻。**空なら締切後も無期限に受け付ける**（従来どおり）。
    #
    # 締切と分ける。締切は「ここから減点が始まる」で、こちらは「ここで
    # 受け付けを終える」であって、間にあるのが**減点提出できる時間**である
    # （#73）。同じ値にすると、遅れた学習者が何も出せなくなる ── 出せない
    # ままでは何を間違えたのかも分からない（ADR 0013）。
    accepts_until: datetime | None = None
    # 成績の自動確定までの猶予（分）。**空なら科目の設定**（`grace_minutes`）。
    auto_finalize_after_minutes: int | None = Field(default=None, gt=0)
    # この課題で受け付ける提出ファイル形式（拡張子）。空なら科目の既定
    # （`aijudge_core.uploads.allowed_suffixes`）。日程と違い、**課題ごとに
    # 決まる**性質である ── 同じ回でもコードで出す問題とレポートで出す問題が
    # 並ぶ。
    accepted_suffixes: tuple[str, ...] = ()
    # 出題を取り下げたか。**削除ではない。**
    #
    # 採点結果は課題版を指しているので（P8）、提出のある課題を消すと過去の
    # 成績が何の課題の点なのか辿れなくなる。知識要素で決めたのと同じ区別で
    # （`aijudge_admin.kc`）、**一度も使われていないものは消せる／使われた
    # ものは取り下げる**。取り下げた課題は学習者に出さないが、記録は残り、
    # 教員の一覧には印付きで並ぶ。押し間違いは取り消せる。
    withdrawn: bool = False

    @model_validator(mode="after")
    def _check_schedule(self) -> Self:
        """日程の前後関係。**壊れた順序を保存させない。**

        提出開始が締切より後の課題は、誰も提出できないまま締切を迎える。
        画面で弾いても API から入りうるので、模型で止める。
        """
        if self.opens_at and self.submissions_open_at and self.submissions_open_at < self.opens_at:
            raise ValueError("提出開始が公開より前になっています")
        if self.due_at and self.submissions_open_at and self.due_at <= self.submissions_open_at:
            raise ValueError("締切が提出開始より前になっています")
        if self.due_at and self.opens_at and self.due_at <= self.opens_at:
            raise ValueError("締切が公開より前になっています")
        # **採点開始が提出開始より前なのは無意味ではなく有害。** 提出が
        # 始まる前に「採点を待つ」状態が作られ、教員が試験モードだと
        # 思っている画面で提出が即座に採点される。
        if (
            self.grading_starts_at
            and self.submissions_open_at
            and self.grading_starts_at < self.submissions_open_at
        ):
            raise ValueError("採点開始が提出開始より前になっています")
        # 受付終了が締切より前だと、減点提出できる時間が負になる。
        if self.accepts_until and self.due_at and self.accepts_until < self.due_at:
            raise ValueError("受付終了が締切より前になっています")
        if (
            self.accepts_until
            and self.submissions_open_at
            and self.accepts_until <= self.submissions_open_at
        ):
            raise ValueError("受付終了が提出開始より前になっています")
        return self

    def accepts_submissions_at(self, now: datetime) -> bool:
        """いま提出を受け付けるか。

        **締切では閉じない。** 遅れた提出は受け付けたうえで減点する
        （ADR 0013）── 受け付けないと、遅れた学習者は何も出せず、何を
        間違えたのかも分からないまま終わる。

        閉じるのは `accepts_until` である。**空なら閉じない**（従来どおり）。
        締切とは別の値で、間にあるのが「減点提出できる時間」になる（#73）。
        """
        opens = self.submissions_open_at or self.opens_at
        if opens is not None and now < opens:
            return False
        return self.accepts_until is None or now <= self.accepts_until

    def submission_window_at(self, now: datetime) -> SubmissionWindow:
        """いまこの課題がどの状態にあるか（#73）。

        **画面の区分そのものである。** 学習者の一覧は「提出できる／減点提出
        できる／提出できない」で分かれ、教員が締切と受付終了をどう置いたかが
        そのまま出る。判定を画面ごとに書くと、一覧と課題ページで食い違う。
        """
        opens = self.submissions_open_at or self.opens_at
        if opens is not None and now < opens:
            return SubmissionWindow.NOT_OPEN
        if self.accepts_until is not None and now > self.accepts_until:
            return SubmissionWindow.CLOSED
        if self.due_at is not None and now > self.due_at:
            return SubmissionWindow.LATE
        return SubmissionWindow.OPEN

    @property
    def unit_label(self) -> str:
        """問題セットの表示名。

        `session` があれば「第 3 回」、無ければ `unit` をそのまま出す
        （`exam08` のような、回に対応しないまとまりがある）。
        """
        if self.session is not None:
            return f"第 {self.session} 回"
        return self.unit or "未分類"

    @property
    def sort_key(self) -> tuple[int, str, int]:
        """一覧の並び順。回がある課題を先に、無いものを後に。"""
        return (
            self.session if self.session is not None else 10**6,
            self.unit or "",
            self.position if self.position is not None else 10**6,
        )
