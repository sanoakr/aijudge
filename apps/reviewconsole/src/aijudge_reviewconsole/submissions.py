"""提出の一覧と絞り込み。

**教員が「実際に何が出ているか」を見る場所。** 待ち行列（再確認の依頼）と
確定処理は、手を動かす必要があるものだけを出す ── そこに全提出を混ぜると、
どちらの画面も使えなくなる（ADR 0009）。一方で、授業の途中で「第 3 回の
p2 はどのくらい通っているか」を見たいことは常にあり、その入口がここである。

**絞り込みは URL に載せる。** 状態をサーバに持たない ── 教員は絞った結果を
そのまま学生や TA に渡すことがあり、リンクで渡せないと画面の説明から
始めることになる。

`adopted`（採用提出だけ）は、学習者に見えている成績と同じ見方をするための
ものである。同じ課題に何度も出すのが学習の形なので、全提出を平らに数えた
分布は「何度も試した人ほど低い点が多い」という形になり、到達度としては
読めない（`aijudge_studentweb.progress` が同じ規則を学習者側で使っている）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aijudge_core import (
    Course,
    Finalization,
    FinalizationSource,
    GradingRun,
    HumanReview,
    ReviewRequest,
    Role,
    Submission,
    Task,
    TaskVersion,
    final_score,
    score_withheld,
)
from aijudge_core.ids import SubmissionId
from aijudge_submission.protocols import ScoredRow

from .overview import unit_key

# 分布の階級。0-9, 10-19, …, 100 の 11 本。**100% は独立させる** ──
# 満点かどうかは教員が最初に見るところで、90 台に混ぜると読めない。
BUCKETS = 11


#: 一覧の 1 頁に出す件数（#255）。実測は 1 行あたり 0.65 KB・0.4 ミリ秒なので、
#: 100 件で 65 KB・0.04 秒 ── いまの 3.19 MB・2.1 秒とは桁が 2 つ違う。
#:
#: **200 にしない。** PC では 6 画面ぶんだが、畳んだ表では 1 件 1 枚（約 100px）
#: なので、携帯の 1 頁が 2 万ピクセルになる（#231）。
#: **50 にもしない。** 「先週の第 3 回」を見るのに何度も送ることになる。
#:
#: 頁数が増えることは問題にならない ── 一覧は新しい順なので、ほしいものは
#: ほぼ常に 1 頁目である。探すときは絞り込みを使う（#247）。
PAGE_SIZE = 100


@dataclass(frozen=True)
class Page:
    """いま何頁目を見ているか（#255）。

    **総数は頁に追従しない。** 「全 N 件」が 1 頁ぶんの数になると、
    コースの大きさが頁送りのたびに変わって見える。
    """

    number: int
    # **その場で読む。** 既定値に `PAGE_SIZE` を直接書くと、クラスを作った
    # 時点の値で固まる ── 件数を差し替えて試せなくなる。
    size: int = field(default_factory=lambda: PAGE_SIZE)
    #: 絞り込みに合う提出の総数。
    total: int = 0

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def first(self) -> int:
        """この頁の先頭が何件目か（1 起点・画面に出す）。"""
        return 0 if not self.total else (self.number - 1) * self.size + 1

    @property
    def last(self) -> int:
        return min(self.number * self.size, self.total)

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages


@dataclass(frozen=True)
class Listing:
    """一覧に出す行と、**それが全部かどうか**（#233）。

    上限に当たったことを持ち回るのは、画面がそれを言う必要があるから
    ── 数の話ではなく、読み手が「これで全部」と思ってよいかの話である。
    """

    rows: list[Row]
    truncated: bool
    limit: int
    page: Page = field(default_factory=lambda: Page(number=1))


@dataclass(frozen=True)
class Row:
    """一覧の 1 行。"""

    submission: Submission
    run: GradingRun | None
    task: Task
    version: TaskVersion
    learner: object | None
    # **提出したときの役割**（#108）。いまの受講から引かない ── 学生が TA に
    # なった瞬間に、その人の過去の提出が絞り込みから消える（ADR 0013 の轍）。
    role: Role
    score: float | None
    finalized_by: FinalizationSource | None
    # **誰が閉じたか**（#102）。自動確定では空。出所（`finalized_by`）だけでは
    # 「誰の判断で成績が閉じたのか」に答えられない ── 記録はあった
    # （`Finalization.actor_id`）が、画面に出ていなかった。
    finalized_by_login: str
    contested: bool
    # この学習者のこの課題で、いちばん点の高い提出か（＝成績に採用される）。
    adopted: bool = False
    #: 総合点を保留しているか（#235）。`score` が `None` になる理由は
    #: 「保留」と「まだ採点が無い」の 2 つあり、**画面はそれを区別する**。
    withheld: bool = False

    @property
    def is_trial(self) -> bool:
        """成績にも統計にも数えない提出か（#108）。"""
        return self.submission.is_trial

    @property
    def unit(self) -> str:
        return unit_key(self.task)

    @property
    def login(self) -> str:
        return getattr(self.learner, "login", "") or str(self.submission.learner_id)

    @property
    def state_label(self) -> str:
        if self.run is None:
            return "採点中"
        if self.finalized_by is FinalizationSource.INSTRUCTOR_REVIEW:
            return "確定（教員が確認）"
        if self.finalized_by is FinalizationSource.INSTRUCTOR_BULK:
            return "確定（一括）"
        if self.finalized_by is FinalizationSource.AUTOMATIC:
            return "確定（自動）"
        if self.contested:
            return "再確認の依頼あり"
        return "確定前"


@dataclass(frozen=True)
class Filters:
    """絞り込みの条件。**URL の問い合わせ文字列がそのまま入る。**"""

    unit: str = ""
    task: str = ""
    learner: str = ""
    role: str = ""
    state: str = ""
    adopted: bool = False

    def matches(self, row: Row) -> bool:
        if self.unit and row.unit != self.unit:
            return False
        if self.task and str(row.task.id) != self.task:
            return False
        # **前方一致。** 受講 91 名の学籍番号を選択肢に並べても選べない。
        if self.learner and not row.login.lower().startswith(self.learner.lower()):
            return False
        if self.role and row.role.value != self.role:
            return False
        if self.state and _state_key(row) != self.state:
            return False
        return not (self.adopted and not row.adopted)

    def matches_scored(self, row: ScoredRow, *, adopted: bool) -> bool:
        """細い行（#253）に同じ絞り込みを効かせる。

        **規則を書き写さない。** 問題セット・問題・学習者は SQL で既に
        絞られている（`scored_for_course` へ同じ引数を渡す）ので、ここに
        残るのは状態・役割・採用提出の 3 つだけである。

        **役割は数える前に決着する。** 図は試行を数えない（#108）ので、
        母数は `is_trial` が偽の提出だけになる。`is_trial` は「学習者以外が
        出した、または デモ」なので、**学習者以外の役割で絞れば母数は必ず
        空**であり、学習者で絞ることは母数を変えない。近似ではなく、
        `Submission.is_trial` の定義からそうなる。

        **この理屈はデモコースでは成り立たない**（#273）。あちらでは試行も
        数えるので、`is_trial` が偽であることを前提にできない ── そして
        `ScoredRow` は `submitted_as` を持たないので、役割では絞れない。
        だから役割で絞っているあいだは試行を数えない（呼び手が決める）。
        """
        if self.role and self.role != Role.LEARNER.value:
            return False
        if self.state and _scored_state(row) != self.state:
            return False
        return not (self.adopted and not adopted)


def _scored_state(row: ScoredRow) -> str:
    """細い行の状態。**`_state_key` と同じ順で判定する。**"""
    if not row.graded:
        return "grading"
    if row.reviewed or row.finalized:
        return "finalized"
    return "contested" if row.contested else "open"


STATE_LABELS: dict[str, str] = {
    "grading": "採点中",
    "open": "確定前",
    "contested": "再確認の依頼あり",
    "finalized": "確定済み",
}


def _state_key(row: Row) -> str:
    if row.run is None:
        return "grading"
    if row.finalized_by is not None:
        return "finalized"
    return "contested" if row.contested else "open"


@dataclass
class Distribution:
    """絞り込んだ結果の分布。**画面に出すのは数字ではなく形である。**

    平均だけを出すと、二山（できた人とまったく手が付かなかった人）が
    片方の数字に潰れる。実際の演習ではそれが最も見たい形なので、階級で
    持つ。
    """

    counts: list[int] = field(default_factory=lambda: [0] * BUCKETS)
    scored: int = 0
    total: int = 0
    #: 試行として落とした件数（#108・#273）。**落とした数を持ち回る。**
    #: 持たないと、画面は「提出が無い」「採点待ち」「全部試行」を同じ
    #: 文言で出すことになる ── 前の 2 つは待てば変わり、最後は変わらない。
    trials: int = 0

    @property
    def peak(self) -> int:
        return max(self.counts) if self.counts else 0

    @property
    def mean(self) -> float | None:
        if not self.scored:
            return None
        return sum(index * 10 * count for index, count in enumerate(self.counts)) / self.scored

    def label(self, index: int) -> str:
        return "100" if index == BUCKETS - 1 else f"{index * 10}"


def distribution_of(rows: list[Row]) -> Distribution:
    """得点の分布。**教員・TA 自身の試行は数えない**（#108）。

    動作確認で通した入力は到達度ではない。混ぜると「この課題は正答率が低い」
    が、実は教員が壊れた入力を試した結果、という形で現れる。
    """
    rows = [row for row in rows if not row.is_trial]
    result = Distribution(total=len(rows))
    for row in rows:
        if row.score is None:
            continue
        result.scored += 1
        percent = max(0.0, min(1.0, row.score)) * 100
        index = BUCKETS - 1 if percent >= 100 else int(percent // 10)
        result.counts[index] += 1
    return result


def load_scored(uow: object, course: Course, filters: Filters) -> tuple[ScoredRow, ...]:
    """図のための細い読み出し（#253）。**一覧とは別の経路。**

    絞り込みは一覧とまったく同じものを渡す ── 図と一覧が違う範囲を描けば、
    読み手はその図を一覧の範囲だと読む。SQL に載らない条件は
    `Filters.matches_scored` が後で絞る。
    """
    return uow.submissions.scored_for_course(  # type: ignore[attr-defined]
        course.id,
        task_ids=_task_ids(uow, course, filters),
        learner_ids=_learner_ids(uow, course, filters),
    )


def distribution_for(
    rows: tuple[ScoredRow, ...], filters: Filters, *, count_trials: bool = False
) -> Distribution:
    """得点の分布を、一覧とは別の読み出しから作る（#253）。

    **一覧に上限が要るのは描くからで、図に上限は要らない**（数えるだけなので
    件数に依らない）。同じ行から作っている限り、描画側の制約がそのまま図の
    母数になる ── 切られた分布がコース全体の分布として読まれ、一覧を頁送りに
    すれば 1 頁ぶんの分布になる（#233・#255）。

    **教員・TA 自身の試行は数えない**（#108）。動作確認で通した入力は到達度
    ではない ── 混ぜると「この課題は正答率が低い」が、実は教員が壊れた入力を
    試した結果、という形で現れる。

    **デモコースだけは数える**（`count_trials`・#273）。`is_trial` は性質の
    違う 2 つを同じ印にしている ── 実コースでの教員の試行（**除外は必須**）と、
    デモコースへの提出（`is_demo`）である。後者には**守るべき到達度が無い**：
    そこは試すための場所で、分布もまた試す対象である。除外すると「試すための
    画面で、試した結果が見られない」という、目的と逆の形になる。

    **ここは画面のためだけの判断である。** 測定（`packages/observation`）にも
    成績にも影響しない ── あちらは `is_trial` をそのまま見る。

    **採用は同点で後の提出を採る**（#256）が、図には効かない ── 同点なら
    どちらを数えても棒の高さは同じである。ここで要るのは「学習者・課題ごとの
    最高点」だけで、その値に曖昧さは無い。
    """
    counted = list(rows) if count_trials else [row for row in rows if not row.is_trial]
    trials = len(rows) - len(counted)

    best: dict[tuple[str, str], float] = {}
    for row in counted:
        if row.final_ratio is None:
            continue
        key = (str(row.learner_id), str(row.task_id))
        current = best.get(key)
        if current is None or row.final_ratio > current:
            best[key] = row.final_ratio

    def is_adopted(row: ScoredRow) -> bool:
        if row.final_ratio is None:
            return False
        return best.get((str(row.learner_id), str(row.task_id))) == row.final_ratio

    counted = [row for row in counted if filters.matches_scored(row, adopted=is_adopted(row))]

    result = Distribution(total=len(counted), trials=trials)
    for row in counted:
        if row.final_ratio is None:
            continue
        result.scored += 1
        percent = max(0.0, min(1.0, row.final_ratio)) * 100
        index = BUCKETS - 1 if percent >= 100 else int(percent // 10)
        result.counts[index] += 1
    return result


#: 一覧が一度に読む上限（#233）。数千件を一度に描かないための仕組みで、
#: **判断には使わない**（`count_for_course` / `list_for_versions`）。
LISTING_LIMIT = 5000


def load_rows(
    uow: object,
    course: Course,
    filters: Filters | None = None,
    *,
    scored: tuple[ScoredRow, ...] | None = None,
    page: int = 1,
) -> Listing:
    """このコースの提出を、採点と人間側の記録まで揃えて読む。

    **1 件ずつ引かない。** 受講 91 名 × 課題十数件 × 再提出で数千件になり、
    提出ごとに 4 回問い合わせると一覧を開くたびにそれを踏む
    （`latest_for_many` / `decisions_for_runs` はそのためにある）。

    **絞り込める条件は読む前に効かせる**（#247）。以前は上限まで読んでから
    すべて Python で絞っていたので、「第 3 回だけ」を見ても読み込み量は
    コース全体のままだった ── しかも上限に当たると、絞り込みは**切り落と
    された後ろ**を探すことになる（教員には「その回の古い提出が無い」と
    見える）。課題版と学習者は行が持っているので、そこは問い合わせに載る。

    載らない条件（`state`・`adopted`・`role`）は読んだ後で絞る形が残るが、
    母数が桁で小さくなるので上限にはまず当たらない。

    **上限に当たったかどうかを返す**（#233）。黙って切ると、教員は「最近の
    提出が無い」のか「切られた」のかを区別できない ── 同じ行から作る得点
    分布も、切られた母数で描いたことが読み手に伝わらない。
    """
    filters = filters or Filters()
    if scored is None:
        scored = load_scored(uow, course, filters)
    adopted = adopted_ids(scored)
    task_ids = _task_ids(uow, course, filters)
    learner_ids = _learner_ids(uow, course, filters)

    # **問い合わせで頁を切れるのは、絞り込みがすべて SQL に載るときだけ**
    # （#255）。状態・役割・採用提出は読んだ後で絞るので、先に頁を切ると
    # 1 頁の件数が絞り込みのぶんだけ減り、頁数も合わなくなる。
    #
    # 載るときが、いちばん効かせたい場合でもある ── 教員が最初に開くのは
    # 絞り込みの無い画面で、そこが 3.19 MB・2.1 秒だった。
    in_sql = not (filters.state or filters.role or filters.adopted)
    number = max(1, page)
    if in_sql:
        # 総数は細い読み出しの長さで足りる ── 図のために既に読んでいるので、
        # 数えるためだけの問い合わせを足さない。
        paging = Page(number=number, total=len(scored))
        number = min(number, paging.pages)
        paging = Page(number=number, total=len(scored))
        submissions = uow.submissions.list_for_course(  # type: ignore[attr-defined]
            course.id,
            limit=paging.size,
            offset=(number - 1) * paging.size,
            task_ids=task_ids,
            learner_ids=learner_ids,
        )
        truncated = False
    else:
        # 1 件多く読んで、上限に当たったかを知る。
        submissions = uow.submissions.list_for_course(  # type: ignore[attr-defined]
            course.id,
            limit=LISTING_LIMIT + 1,
            task_ids=task_ids,
            learner_ids=learner_ids,
        )
        truncated = len(submissions) > LISTING_LIMIT
        submissions = submissions[:LISTING_LIMIT]
        paging = Page(number=number)
    runs = uow.runs.latest_for_many([s.id for s in submissions])  # type: ignore[attr-defined]
    decisions = uow.reviews.decisions_for_runs([r.id for r in runs.values()])  # type: ignore[attr-defined]

    versions: dict[str, TaskVersion] = {}
    tasks: dict[str, Task] = {}
    for task in uow.tasks.list_for_course(course.id):  # type: ignore[attr-defined]
        tasks[str(task.id)] = task
    learners: dict[str, object] = {}
    # 確定者・レビュー者。受講者とは限らない（管理者が閉じることがある）ので
    # 受講の一覧からは引けない。**同じ人を何度も引かない。**
    actors: dict[str, str] = {}

    rows: list[Row] = []
    for submission in submissions:
        version = versions.get(str(submission.task_version_id))
        if version is None:
            version = uow.tasks.get_version(submission.task_version_id)  # type: ignore[attr-defined]
            if version is None:
                continue
            versions[str(version.id)] = version
        task = tasks.get(str(version.task_id))
        if task is None:
            continue
        learner_id = str(submission.learner_id)
        if learner_id not in learners:
            learners[learner_id] = uow.identity.get_user(submission.learner_id)  # type: ignore[attr-defined]

        run = runs.get(submission.id)
        decision = None if run is None else decisions.get(run.id)
        review: HumanReview | None = None if decision is None else decision.review
        request: ReviewRequest | None = None if decision is None else decision.request
        finalization: Finalization | None = None if decision is None else decision.finalization
        rows.append(
            Row(
                submission=submission,
                run=run,
                task=task,
                version=version,
                learner=learners[learner_id],
                role=submission.submitted_as,
                # **保留は 0% ではない**（#235）。`FinalScore.final` は
                # 保留でも 0.0 を返すので、そのまま出すと学習者に「保留」と
                # 見えている提出が、教員には「0 点の答案」として見える ──
                # しかもこの値は採用提出の選定と得点分布に数として入る。
                score=(
                    None
                    if run is None or score_withheld(run, review)
                    else final_score(run, version, review).final
                ),
                withheld=run is not None and score_withheld(run, review),
                finalized_by=_finalized_by(finalization, review),
                finalized_by_login=_actor_login(finalization, review, uow, actors),
                contested=request is not None and not request.resolved,
            )
        )
    rows = _apply_adopted(rows, adopted)
    if not in_sql:
        # SQL で頁を切れなかったぶんは、絞ってからここで切る。総数も
        # 絞った後の数で数える ── 頁の数と一覧の中身が食い違わない。
        rows = newest_first([row for row in rows if filters.matches(row)])
        paging = Page(number=number, total=len(rows))
        number = min(number, paging.pages)
        paging = Page(number=number, total=len(rows))
        rows = rows[(number - 1) * paging.size : number * paging.size]
    return Listing(rows=rows, truncated=truncated, limit=LISTING_LIMIT, page=paging)


def _task_ids(uow: object, course: Course, filters: Filters) -> list[object] | None:
    """`unit` / `task` を課題の並びに直す（#247）。

    絞っていなければ `None`（＝絞らない）。**空の列は返しうる**が、それは
    「条件に当たる課題が無い」であって「絞らない」ではない ── 保存層は
    その区別を守る。

    **課題で返す。課題版ではない。** 提出は出したときの版を指すので、
    最新版だけで絞ると、課題を直す前に出した提出が一覧から消える。
    """
    if not filters.unit and not filters.task:
        return None
    return [
        task.id
        for task in uow.tasks.list_for_course(course.id)  # type: ignore[attr-defined]
        if not (filters.task and str(task.id) != filters.task)
        and not (filters.unit and unit_key(task) != filters.unit)
    ]


def _learner_ids(uow: object, course: Course, filters: Filters) -> list[object] | None:
    """学習者の前方一致を利用者の並びに直す（#247）。

    一覧が絞るのは **login の前方一致**で、login は提出ではなく利用者の側に
    ある。テナントの利用者を 1 回読んで突き合わせる ── 受講から引くと、
    学生が TA になった後に過去の提出が消える（#108 と同じ轍）。
    """
    prefix = filters.learner.strip().lower()
    if not prefix:
        return None
    return [
        user.id
        for user in uow.identity.list_all_users(course.tenant_id)  # type: ignore[attr-defined]
        if (user.login or "").lower().startswith(prefix)
    ]


def _actor_login(
    finalization: Finalization | None,
    review: HumanReview | None,
    uow: object,
    cache: dict[str, str],
) -> str:
    """成績を閉じた人の login。**読んだ人を優先する**（ADR 0010）。

    一括確定と自動確定は誰も読んでいない。前者には操作した教員が居るので
    その人を出し、後者は空にする ── 人が居ない確定に人の名前を出すと、
    「その教員が確認した」と読めてしまう。
    """
    actor_id = None
    if review is not None:
        actor_id = review.grader_id
    elif finalization is not None:
        actor_id = finalization.actor_id
    if actor_id is None:
        return ""
    key = str(actor_id)
    if key not in cache:
        user = uow.identity.get_user(actor_id)  # type: ignore[attr-defined]
        cache[key] = getattr(user, "login", "") or key
    return cache[key]


def _finalized_by(
    finalization: Finalization | None, review: HumanReview | None
) -> FinalizationSource | None:
    """確定の出所。**教員が読んでいればそう示す**（ADR 0010）。"""
    if review is not None:
        return FinalizationSource.INSTRUCTOR_REVIEW
    return None if finalization is None else finalization.source


def adopted_ids(rows: tuple[ScoredRow, ...]) -> set[SubmissionId]:
    """学習者・課題ごとに、成績に採用される提出（#256）。

    **規則はここ 1 つだけ。** 学習者側（`aijudge_studentweb.progress`）と
    同じでなければならない ── 成績に採用される提出が画面によって違えば、
    教員は問い合わせに答えられない。

    点がいちばん高いもの。**同点なら後に出した提出**を採る。比べるのは
    (点, 提出時刻, 回数) で、時刻だけでは同じ時刻に入った提出が偶然で決まる。

    **並び順に依存させない。** 以前は「先に見つけた方を後で上書きする」
    書き方で、入力が古い順であることに暗黙に頼っていた ── 一覧を新しい順に
    変えたとき、同点の採用が学習者と逆になった（#233・#256）。

    **細い行から決める**（#253）。頁送りが入ると一覧は 1 頁ぶんしか持たない
    ので、そこから採用を決めると「この頁の中でいちばん高い提出」になる
    （#255）。採用はコース全体で決まる事実である。
    """

    def rank(row: ScoredRow) -> tuple[float, datetime, int]:
        return (row.final_ratio or 0.0, row.submitted_at, row.attempt)

    best: dict[tuple[str, str], ScoredRow] = {}
    for row in rows:
        if row.final_ratio is None:
            # 点の無い提出（採点中・保留）は候補にしない ── 保留を採用として
            # 示すと、点が付いていないことが「0 点が採用された」に見える。
            continue
        key = (str(row.learner_id), str(row.task_id))
        current = best.get(key)
        if current is None or rank(row) > rank(current):
            best[key] = row
    return {row.submission_id for row in best.values()}


def _apply_adopted(rows: list[Row], adopted: set[SubmissionId]) -> list[Row]:
    """採用の印を付ける。**決めるのは `adopted_ids`、ここは写すだけ。**"""
    for index, row in enumerate(rows):
        if row.submission.id not in adopted:
            continue
        rows[index] = Row(
            submission=row.submission,
            run=row.run,
            task=row.task,
            version=row.version,
            learner=row.learner,
            role=row.role,
            score=row.score,
            finalized_by=row.finalized_by,
            finalized_by_login=row.finalized_by_login,
            contested=row.contested,
            adopted=True,
            withheld=row.withheld,
        )
    return rows


def newest_first(rows: list[Row]) -> list[Row]:
    return sorted(
        rows,
        key=lambda row: row.submission.submitted_at or row.submission.created_at,
        reverse=True,
    )


def summarize(rows: list[Row]) -> dict[str, object]:
    """コースのメニューに出す 1 行ぶんの概要。

    **試行は数えない**（#108）── ここに出るのは「何人が何件出したか」で、
    教員自身の動作確認はその問いの答えではない。
    """
    rows = [row for row in rows if not row.is_trial]
    latest: datetime | None = None
    for row in rows:
        at = row.submission.submitted_at or row.submission.created_at
        if latest is None or at > latest:
            latest = at
    return {
        "total": len(rows),
        "learners": len({str(row.submission.learner_id) for row in rows}),
        "latest": latest,
    }


__all__ = [
    "BUCKETS",
    "STATE_LABELS",
    "Distribution",
    "Filters",
    "Row",
    "ScoredRow",
    "adopted_ids",
    "distribution_for",
    "distribution_of",
    "load_rows",
    "load_scored",
    "newest_first",
    "summarize",
]
