"""デモコースを消して作り直す（#194）。

**「リセット」は器ごと作り直す。** 中身だけ空にするのではない ── 当初の
問題セットも戻すので、作り直すほうが「学期の初めの状態」に一致する。

これができるのは**コースの ID が決定的だから**である（`course_id_for` が
テナント・コード・学期から導く）。消して作り直しても同じ ID になるので、
学生が開いていた URL は死なない ── 中身だけ空にする案の利点として挙げた
ものが、作り直しでもそのまま得られる。

**テナント管理者だけの操作。** 担当教員には出さない ── 誰かの提出を
まとめて消す操作で、しかもデモコースは全員のものだからである。

**自動では走らせない**（#194 の判断）。膨れ上がりはコンソールのコース一覧
（問題セット・課題・受講者の数）から見えるので、人が見て叩けば足りる。
消す操作を自動で回す仕掛けを一度作ると、それが本物のコースに向く事故の
余地が残る。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aijudge_core import Course
from aijudge_core.ids import TenantId
from aijudge_identity import DemoCourse
from aijudge_persistence import Database

from .courses import delete_course
from .operations import AdminError, ensure_course

__all__ = ["DemoReset", "reset_demo_course"]


@dataclass(frozen=True)
class DemoReset:
    """何を消して、何を作り直したか。

    **数を返す。** 「消しました」だけだと、何も無いところで空振りしたのか
    90 件消したのかが分からない ── どちらも同じ顔で終わってはいけない。
    """

    course: Course
    submissions: int
    tasks: int
    enrolments: int
    recreated: bool


def reset_demo_course(
    database: Database,
    demo: DemoCourse,
    *,
    tenant_id: TenantId,
    profiles_dir: Path,
    artifact_store: object | None = None,
) -> DemoReset:
    """デモコースを消して、同じ素性で作り直す。

    **提出も受講登録も消える。** 受講登録は次のログインで自動的に戻るので
    （`enrol_into_demo_course`）、消しても誰も締め出されない ── ただし
    「教員が学生として入っている」状態も消えるため、その人は次のログインで
    教員に戻る。デモコースの役割は入り口で決まるものなので、初期状態に
    戻すのが筋である。

    **デモコースでなければ拒む。** 環境変数が指しているコースだけを対象に
    する ── ここを指定で受けると、打ち間違いで本物のコースが消える。
    """
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(demo.course_id)
    if course is None:
        raise AdminError(f"デモコース {demo.course_id} がありません")
    if course.tenant_id != tenant_id:
        raise AdminError("このテナントのコースではありません")

    deleted = delete_course(database, course_id=demo.course_id, artifact_store=artifact_store)

    # **同じ素性で作り直す。** ID はテナント・コード・学期から導かれるので、
    # 同じ値を渡せば同じ ID になる（`course_id_for`）── 学生が開いていた
    # URL は生き続ける。
    recreated, created = ensure_course(
        database,
        tenant_id=tenant_id,
        code=course.code,
        title=course.title,
        term=course.term,
        subject_profile=course.subject_profile,
        profiles_dir=profiles_dir,
    )
    if recreated.id != course.id:  # pragma: no cover - 導出が変わったときだけ
        raise AdminError(
            "作り直したコースの ID が変わりました。学生が開いている URL が死ぬので、中断します"
        )
    return DemoReset(
        course=recreated,
        submissions=deleted.trial_submissions,
        tasks=deleted.tasks,
        enrolments=deleted.enrolments,
        recreated=created,
    )
