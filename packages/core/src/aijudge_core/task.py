"""課題（Task）とルーブリック。

TaskVersion を不変にしているのは採点の再現性（P8）のため。
問題文やルーブリックが直った後に過去の採点を読み返しても、
そのとき何を基準に採点したのかが必ず分かる。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .ids import CourseGroupId, CourseId, CriterionId, TaskId, TaskVersionId, UserId
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
    # **この課題を採点するプロファイル**（#195）。どの評価器がどの順で走るか。
    #
    # 以前は常にコースの値の写しが入り、誰にも読まれていなかった。その結果、
    # 1 つのコースに種類の違う課題を置けなかった ── レポートとプログラムが
    # 混在する科目は実在するのに、2 コースに割るしかなかった。
    #
    # **コースのプロファイルは残る。役割が違う**（ADR 0018）。コースは語彙
    # （`kc_namespaces`）と新しい課題の既定を決め、課題は採点を決める。
    # 評価器の指名は「何を採点するか」で決まるのだから、課題に付く。
    subject_profile: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    reference_solution: str | None = None
    # **並びが評価順である。** AND のときは上から評価して 0% で打ち切る。
    criteria: tuple[RubricCriterion, ...] = Field(min_length=1)
    # 観点の畳み方。None ならコースの設定に従う（`effective_aggregation`）。
    aggregation: Aggregation | None = None
    test_cases: tuple[TestCase, ...] = ()
    q_matrix: tuple[QMatrixEntry, ...] = ()
    # **配点**（2026-09-25 決定）。提出の得点は「割合 × この値」で、問題セットの
    # 合計とクリアの判定に使う。**版に付く** ── 配点を変えるには版を上げる。
    # 教員が配点を下げても、前の版で取った点は下がらない（学生にとって公平）。
    # 版をまたいだ採用は点数で比べる。
    max_score: float = Field(gt=0.0)
    # この版の `max_score` を**配点として書いたか**。配点の機能より前に作られた版は
    # 既定の 100 が入っているだけで、一度も点数に使われていない（偽のまま読まれる）。
    # そうした版は、その課題で最初に配点を書いた版の値で数える
    # （`effective_max_score`）── 既出の問題にあとから配点を入れても、学生に
    # 見せていたのは割合だけなので、以前の提出もその配点で数えて揃える。
    #
    # **不変性の比較から外す**（`aijudge_authoring.VOLATILE_FIELDS`）。外さないと、
    # 既存の版を同じ内容で入れ直すたびに「内容が違う」になり、版が増えるか断られる。
    points_declared: bool = False
    allow_handwriting: bool = False
    # 書き起こしを**学習者に確認させるか**（ADR 0018: 採点のされ方は課題が決める）。
    #
    # **既定は False。** 確認を求めるのは、学習者が直せるもの ── 手書き答案を
    # 撮って出す流れ（Phase 6）である。そこでは学習者が確定させた時点で内容の
    # 責任が移り、「OCR が間違えたせいで減点された」という異議の構造が消える。
    #
    # 認定証の画像のように**学習者が直しようのないもの**では求めない。出しても
    # 押させるだけの儀式になり、しかも「確認した」という記録だけが残って
    # 責任の所在を偽る。
    #
    # **True にする経路はまだ無い**（確認画面が未実装）。画面には出すが変更
    # させない ── 値の意味と置き場所を先に決めておかないと、画面を作る日に
    # 課題の版を作り直すことになる。
    confirm_transcription: bool = False
    # この版を作った `TaskSpec.key`。**訂正のときに要る** ── ID は鍵から
    # 導いてあり（`derived_id`）、鍵が無いと次の版の ID も観点の ID も
    # 作れない。古い版には入っていないので None を許す。
    source_key: str | None = None
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def _check_transcription_confirmation(self) -> Self:
        """**確認画面が無いうちは True を受け付けない。**

        画面で灰色にしただけでは境界にならない（#146 の教訓 ── 参照されて
        いるプロファイルを編集できないようにした画面と同じで、**保存の側でも
        checks を通す**）。ここで塞ぐのは、課題を作る経路が 3 つあるため
        （`course apply`・教員コンソール・取り込み）で、画面だけを直しても
        残り 2 つが素通りする。

        True で保存できてしまうと、その課題の提出は確認待ちのまま進めず、
        **誰も採点できない提出が溜まる。**
        """
        if self.confirm_transcription:
            raise ValueError(
                "confirm_transcription is not supported yet: "
                "the learner-facing confirmation screen does not exist, "
                "so submissions would wait for a confirmation that cannot happen"
            )
        return self

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


class AnswerMode(StrEnum):
    """学習者がどう答えるか（ADR 0026）。

    **採点は答え方を知らない**（不変条件 I2）。どちらで出しても同じ
    `Submission` になり、評価器は出どころを区別できない。ここが決めるのは
    学習者の画面だけである。
    """

    # 既存。ファイルを選んで出す。
    UPLOAD = "upload"
    # ブラウザのエディタで書いて出す（`docs/design/online-coding-test.md`）。
    EDITOR = "editor"


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
    #
    # **予定の時刻はどれもタイムゾーン付きに限る**（#401）。素の値が入ると
    # 提出時刻（UTC）との比較が TypeError になり、その課題の採点が全件
    # 失敗する。何時の意味かも決まらない（UTC か機関の時刻か）ので、
    # 推測せずに入口で拒む。
    opens_at: AwareDatetime | None = None
    # 提出を受け付け始める時刻。**空なら公開と同時に受け付ける。**
    # 公開と分けるのは、課題文を先に配って提出は演習時間に開ける運用が
    # あるため。ここが未来なら提出は受け付けない（学習者側で拒否する）。
    submissions_open_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
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
    grading_starts_at: AwareDatetime | None = None
    # 提出の受付を終える時刻。**空なら締切後も無期限に受け付ける**（従来どおり）。
    #
    # 締切と分ける。締切は「ここから減点が始まる」で、こちらは「ここで
    # 受け付けを終える」であって、間にあるのが**減点提出できる時間**である
    # （#73）。同じ値にすると、遅れた学習者が何も出せなくなる ── 出せない
    # ままでは何を間違えたのかも分からない（ADR 0013）。
    accepts_until: AwareDatetime | None = None
    # 成績の自動確定までの猶予（分）。**空なら科目の設定**（`grace_minutes`）。
    auto_finalize_after_minutes: int | None = Field(default=None, gt=0)
    # この課題で受け付ける提出ファイル形式（拡張子）。空なら科目の既定
    # （`aijudge_core.uploads.allowed_suffixes`）。日程と違い、**課題ごとに
    # 決まる**性質である ── 同じ回でもコードで出す問題とレポートで出す問題が
    # 並ぶ。
    accepted_suffixes: tuple[str, ...] = ()
    # 学内からだけ受け付けるか（#333）。**日程と同じ性質の値である** ──
    # 課題が持つが、値を決めるのは問題セット単位で、画面もそう作ってある。
    #
    # **既定は制限しない。** 付け忘れて誰も出せない、より、付け忘れて外からも
    # 出せる、のほうが取り返しがつく（前者は試験当日に全員が止まる）。
    #
    # 何を学内と見なすかはここに書かない ── テナント管理者が設定する
    # （`CampusNetworkSettings`・このリポジトリは公開物である）。
    campus_only: bool = False
    # 公開（`opens_at`）までは教員にしか見せないか。**試験のための値である。**
    #
    # 公開前の問題セットは教員と TA の両方に見えていた（#340 の学生画面・
    # #102 のコンソール）。**課題なら正しい** ── TA が先に読んで質問対応と
    # 採点に備えられる。だが試験では、TA（多くは学生）が内容を先に知ること
    # 自体が漏洩の経路になる。課題と試験を区別する値が無かった。
    #
    # **公開後は TA に見せる。** 学習者に出ているものであり、TA が質問に
    # 答えて採点するには読めなければならない。値の効く区間は公開前だけ。
    #
    # 種別（「試験」）ではなく独立した値にしてある ── 採点保留も学内限定も
    # この値も、それぞれ単独で要る場面がある（`docs/design/task-visibility.md`
    # §1.3）。判定は `aijudge_core.access.may_see` の 1 か所で行う。
    confidential_until_open: bool = False
    # 答え方（ADR 0026）。**学内限定と同じ性質の値である** ── 課題が持つが、
    # 値を決めるのは問題セット単位で、画面もそう作る。
    #
    # **既定は `upload`（従来どおり）。** `tasks.document` に入るので列は増えず、
    # 鍵を持たない既存の課題はこの既定で読まれる ── 今日までの見え方は
    # 変わらない（不変条件 I4・I5）。`editor` にできるのはテストを走らせる
    # 課題だけで、その検査は保存する側（コンソール）が行う。
    answer_mode: AnswerMode = AnswerMode.UPLOAD
    # エディタで補完を出すか（設計書 §5.3・2026-09-24 決定）。**答え方とは独立した
    # 値である** ── 同じ「エディタで解く」でも、試験では切り、演習では入れる。
    # 学内限定・秘匿と同じく、課題が持つが値を決めるのは問題セット単位。
    #
    # **既定は切**（括弧を閉じる・字下げ・色分けだけ）。入れると、そのファイル内の
    # 単語と言語のキーワードを候補に出す。AI の補完や意味を解する補完は作らない。
    # `answer_mode` が `upload` の課題では何もしない（エディタが無い）。
    editor_completion: bool = False
    # ファイルを選んで提出できるか（2026-09-24 決定）。**答え方とは独立した値である**
    # ── `editor` の課題でもファイルの提出欄は既定で残す（エディタが使えない環境の
    # 逃げ道）。切ると「エディタだけ」になり、試験で作業の記録を経ない提出を断てる。
    #
    # **既定は真**（従来どおり）。`upload` の課題で切ることはできない（何も提出
    # できなくなる）── 下の `_check_answer_paths` が止める。切ったときの境界は
    # 提出の受付（学生画面の関門）で、画面から欄を消すだけでは境界にならない（#146）。
    file_upload: bool = True
    # 問題セットのクリア点（2026-09-25 決定）。**問題セットの値**で、学内限定と同じく
    # 課題が持つが決めるのはセット単位（全課題に同じ値）。セット内の各問題の得点
    # （割合 × 配点・`effective_max_score`）の合計がこの値以上ならクリア。
    # None はクリアの条件なし（従来どおり）。表示だけに使い、採点は変えない。
    clear_points: float | None = Field(default=None, gt=0.0)
    # 試験中に画面全体の静止画を撮るか（ADR 0027・#444）。**学内限定と同じ性質の
    # 値である** ── 課題が持つが、値を決めるのは問題セット単位。真の課題は、
    # 画面全体を共有しないと IDE で受験を始められず、共有を止めると手動の提出が
    # 止まる。**既定は撮らない**（演習では撮らない）。`tasks.document` に入るので
    # 列は増えない。
    screen_capture: bool = False
    # 出題先（追試など）。**空は受講者全員**（従来どおり）。複数を持てば、
    # いずれかの名簿に入っている学習者に出す（和集合）。
    #
    # **最初から複数にしてある。** グループごとに別の問題セットを出す運用が
    # 見込まれる（2026-09-24）── 「X は 1 組、Y は 2 組、共通問題は両方」を
    # 課題ごとの指定だけで表せる。単数だと共通問題のためにグループを合成するか、
    # 保存済みの文書を書き換える移行が要る。
    #
    # 出題先は**学習者にだけ効く**（`aijudge_core.access.may_see`）。教員・TA は
    # 名簿に関係なく見える ── TA が追試の質問に答えられないと困る。
    audience_group_ids: tuple[CourseGroupId, ...] = ()
    # 出題を取り下げたか。**削除ではない。**
    #
    # 採点結果は課題版を指しているので（P8）、提出のある課題を消すと過去の
    # 成績が何の課題の点なのか辿れなくなる。知識要素で決めたのと同じ区別で
    # （`aijudge_admin.kc`）、**一度も使われていないものは消せる／使われた
    # ものは取り下げる**。取り下げた課題は学習者に出さないが、記録は残り、
    # 教員の一覧には印付きで並ぶ。押し間違いは取り消せる。
    withdrawn: bool = False

    @model_validator(mode="after")
    def _check_answer_paths(self) -> Self:
        """提出する道が 1 つは残っているか。エディタも無くファイルも断る課題は、
        誰も提出できない。"""
        if self.answer_mode is AnswerMode.UPLOAD and not self.file_upload:
            raise ValueError("エディタを使わない課題では、ファイルの提出を止められません")
        return self

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

    def before_open_at(self, now: datetime) -> bool:
        """公開（`opens_at`）より前か。**空なら公開済み**として扱う（従来どおり）。

        提出開始（`submissions_open_at`）とは別の時刻である。公開から提出開始
        までは「予告」で、学習者は問題文を読めるが出せない。
        """
        return self.opens_at is not None and now < self.opens_at

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


def effective_max_score(version: TaskVersion, history: Iterable[TaskVersion]) -> float:
    """この版の提出を数えるときの配点。

    配点を書いた版（`points_declared`）はその値。書いていない版は:

    - それより前に配点を書いた版があれば、**直近のもの**の値（書き忘れた訂正で
      配点が既定の 100 に戻らない）
    - 無ければ、その課題で**最初に**配点を書いた版の値（配点の機能より前の提出を、
      あとから入れた配点で揃える。学生に見せていたのは割合だけ）
    - どの版にも無ければ、その版の値（既定 100）

    `history` はその課題の版（順不同・`version` を含んでいてよい）。
    """
    if version.points_declared:
        return version.max_score
    declared = sorted(
        (v for v in history if v.task_id == version.task_id and v.points_declared),
        key=lambda v: v.version,
    )
    if not declared:
        return version.max_score
    before = [v for v in declared if v.version < version.version]
    return (before[-1] if before else declared[0]).max_score


def max_scores_by_version(versions: Iterable[TaskVersion]) -> dict[TaskVersionId, float]:
    """版 → その版の提出を数える配点（`effective_max_score`）。課題が混ざっていてよい。

    一覧（課題数 × 版数）で配点を引くたびに履歴を渡し直さないための表。
    """
    by_task: dict[TaskId, list[TaskVersion]] = {}
    for version in versions:
        by_task.setdefault(version.task_id, []).append(version)
    return {
        version.id: effective_max_score(version, history)
        for history in by_task.values()
        for version in history
    }
