"""課題を足す操作。**画面・API・CLI がここを共有する。**

経路ごとに組み立て方が分かれると、「画面から作った課題だけ観点が 1 つ
足りない」が起きる。実際に起きた ── zip 取り込みだけ `readability_weight` が
0.0 固定で、画面から入れた課題には AI 観点が付かなかった（`TaskSpec` の
docstring 参照）。

**冪等。** 同じ `key` に同じ内容を入れ直しても課題は増えず、締切も消えない。
移行では同じディレクトリを何度も流すことになるので、これは必須の性質である。
内容が違う場合は拒否する ── 過去の採点基準を書き換えないため（P8）。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_authoring import TaskSpec, build_task_version
from aijudge_authoring.repository import TaskStoreError, substantive
from aijudge_core import Task, TaskVersion
from aijudge_core.ids import CourseId, UserId
from aijudge_persistence import Database

from .kc import assert_registered
from .operations import AdminError
from .rubric import from_stored


@dataclass(frozen=True)
class SavedTask:
    """保存の結果。**何が起きたかを呼び出し元に返す。**

    「保存しました」だけだと、流し込みが 100 件のうち何件を新しく作ったのか
    運用者に分からない。移行を二度流したときに気づけるようにする。
    """

    task: Task
    version: TaskVersion
    created: bool
    """この課題がこの呼び出しで初めて作られたか。"""

    @property
    def test_cases(self) -> int:
        return len(self.version.test_cases)

    @property
    def auto_graded(self) -> bool:
        return bool(self.version.test_cases)


def save_task(
    database: Database,
    *,
    course_id: CourseId,
    spec: TaskSpec,
    subject_profile: str,
    authored_by: UserId,
    revise: bool = False,
    course_rubric: tuple[dict, ...] = (),
    generated_by: str | None = None,
    generation_prompt_version: str | None = None,
) -> SavedTask:
    """課題を保存する。既にあれば内容の同一性を確かめ、無ければ作る。

    `revise=True` は**訂正**。出題済みの版は書き換えず、版を 1 つ上げて
    新しい `TaskVersion` を作る（P8）。過去の採点がどの基準で付いたのかを
    後から辿れなくなるので、既存の版に上書きはしない。内容が同じなら
    版は上がらない（何度押しても増えない）。
    """
    # **登録済みの KC しか名指しできない**（`kc.assert_registered`）。
    # 綴り違いが静かに新しい KC を作ると、Q-matrix は同じものを 2 つに
    # 割ったまま habits を積み上げる（設計原則 P6 が壊れる）。
    # **課題の宣言が勝つ。** 宣言が無ければコースの共通ルーブリック、
    # それも無ければ組み込みの既定（正しさ＋読みやすさ）。
    if not spec.criteria and course_rubric:
        # 観点を宣言する課題では `readability_weight` は使えない（両方書けると
        # どちらが効くのか読めない・`TaskSpec` の検証）。読みやすさを入れたい
        # なら、共通ルーブリックの観点として書く。
        spec = spec.model_copy(
            update={"criteria": from_stored(course_rubric), "readability_weight": 0.0}
        )

    assert_registered(database, spec.knowledge_components)
    # **出所を落とさない。** `generated_by` を渡さないと `Provenance` は
    # 「教員が書いた」になり、版は承認待ちにならずそのまま出題可能になる
    # （`ReviewState`）。生成物が誰の検査も通らずに出る経路ができる（P5）。
    version = build_task_version(
        spec,
        subject_profile=subject_profile,
        authored_by=authored_by,
        generated_by=generated_by,
        generation_prompt_version=generation_prompt_version,
    )
    if revise:
        with database.unit_of_work() as uow:
            latest = uow.tasks.latest_version(version.task_id)
        if latest is not None:
            if substantive(latest) == substantive(version):
                # 直すつもりで何も変えなかった場合。版を増やさない。
                with database.unit_of_work() as uow:
                    task = uow.tasks.get_task(version.task_id)
                assert task is not None
                return SavedTask(task=task, version=latest, created=False)
            version = build_task_version(
                spec,
                subject_profile=subject_profile,
                authored_by=authored_by,
                version=latest.version + 1,
            )
    with database.unit_of_work() as uow:
        existing = uow.tasks.get_task(version.task_id)
        task = Task(
            id=version.task_id,
            course_id=course_id,
            title=spec.title or _title_of(spec),
            unit=spec.unit,
            session=spec.session,
            position=spec.position,
            # **入れ直しで締切を消さない。** 教員が画面で入れた値を、流し込みの
            # 再実行が黙って消すと成績の期限が飛ぶ。明示された場合だけ上書きする。
            opens_at=spec.opens_at or (existing.opens_at if existing else None),
            due_at=spec.due_at or (existing.due_at if existing else None),
        )
        uow.tasks.save_task(task)
        try:
            uow.tasks.save_version(version)
        except TaskStoreError as exc:
            raise AdminError(
                f"{spec.key}: 保存済みの課題と内容が違います。問題文を直したなら"
                f"版を上げる必要があります（過去の採点基準は書き換えない、P8）: {exc}"
            ) from exc
        uow.commit()

    return SavedTask(task=task, version=version, created=existing is None)


def _title_of(spec: TaskSpec) -> str:
    """問題文の見出しから題名を取る。取れなければキーで代用する。

    題名が無いと一覧が課題 ID の羅列になり、教員がどれを触っているのか
    分からなくなる。
    """
    from aijudge_authoring.importers.sharif_judge import ImportError_, parse_title

    try:
        title, _tag = parse_title(spec.statement)
    except ImportError_:
        return spec.key
    return title


__all__ = ["SavedTask", "save_task"]
