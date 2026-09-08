"""コースを複製する（#170）。

学期をまたいで同じ授業を立ち上げる操作。**コード・コース名・学期だけを
指定し**、問題セットと設定は引き継ぎ、受講登録は空にする。

**課題は宣言（`TaskSpec`）に戻してから作り直す。** 行をコピーしない ──
`TaskSpec` が課題を足す唯一の入口である、という建て付けを崩さないため
（`aijudge_authoring.spec` の冒頭）。行をコピーする経路を作ると、その経路
だけがルーブリックの組み立てを飛ばす形になり、過去に実際そうなった。

引き継がないもの:

  日程        元の学期の日付を持ち込むと、新学期の初日に全課題が締切済みに
              なる。複製直後は日程未定として画面に出す（教員の指定）
  未承認の課題 「まだ出すと決めていないもの」を新しい学期に自動で持ち込む
              理由がない
  受講登録    学期が変われば履修者は変わる
  提出・採点・確定  コース固有の記録で、複製先に持ち込む意味がない

**承認状態は引き継ぐ。** 元は同じ教員のコースで、中身はその人が読んで承認
したものである（zip 取り込み・#161 が未承認から始まるのは「他所で書かれ、
この画面で誰も読んでいない」からで、事情が違う）。

**画像は複製先の鍵へコピーし、課題文の URL を書き換える。** 画像の鍵と URL は
コース単位で（`statement-images/<course>/<name>`）、返す側は URL 中のコース ID で
「採点できる人か」を判定する（`aijudge_reviewconsole.app` の `/images/...`）。
書き換えないと、複製先の教員・TA・学習者にはその画像が 404 になり、元コースを
消せば全員に消える。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aijudge_authoring import TaskSpec
from aijudge_authoring import images as statement_images
from aijudge_core import Course, ReviewState
from aijudge_core.ids import CourseId, UserId
from aijudge_persistence import Database

from . import rubric
from .authoring import save_task
from .operations import AdminError, course_id_for, ensure_course


@dataclass(frozen=True)
class DuplicatedCourse:
    """複製の結果。**何をどれだけ写したかを返す。**

    件数だけでは「写らなかったものがある」ことを言えない（`DeletedCourse` と
    同じ理由）。
    """

    course: Course
    tasks: int
    images: int
    #  写さなかった課題と、その理由。**黙って減らさない。**
    skipped: tuple[tuple[str, str], ...] = ()


def duplicate_course(
    database: Database,
    *,
    source_id: CourseId,
    code: str,
    title: str,
    term: str,
    authored_by: UserId,
    profiles_dir: Path,
    artifact_store: object | None = None,
) -> DuplicatedCourse:
    """コースを複製する。**同じ (コード, 学期) では作れない。**

    コースの同一性は (テナント, コード, 学期) なので、同じ組を指定すると
    複製先の ID が既存のコースと一致する ── そのまま作ると**元のコースを
    上書きする**。先に確かめて断る。
    """
    with database.unit_of_work() as uow:
        source = uow.identity.get_course(source_id)
        if source is None:
            raise AdminError(f"コース {source_id!r} がありません")

    target_id = course_id_for(source.tenant_id, code, term)
    if target_id == source_id:
        raise AdminError(
            "コードと学期が元のコースと同じです。"
            "コースはこの 2 つで見分けているので、どちらかを変えてください。"
        )
    with database.unit_of_work() as uow:
        if uow.identity.get_course(target_id) is not None:
            raise AdminError(
                f"コード {code!r}・学期 {term!r} のコースは既にあります。"
                "複製先には使っていない組み合わせを指定してください。"
            )

    # 学期の形と科目プロファイルの実在は `ensure_course` が確かめる（#167）。
    # **検査を書き写さない** ── 画面と CLI とここで別々に確かめると、
    # 増やした検査が片方に入らない日が来る。
    target, _created = ensure_course(
        database,
        tenant_id=source.tenant_id,
        code=code,
        title=title,
        term=term,
        subject_profile=source.subject_profile,
        profiles_dir=profiles_dir,
    )

    # 設定を丸ごと引き継ぐ。**列を並べない** ── 並べると、コースに設定を
    # 足した日に複製だけが古い一覧のままになる（実際に起きやすい形）。
    carried = source.model_copy(
        update={
            "id": target.id,
            "code": target.code,
            "title": target.title,
            "term": target.term,
        }
    )
    with database.unit_of_work() as uow:
        uow.identity.save_course(carried)
        uow.commit()

    copied, images_copied, skipped = _copy_tasks(
        database,
        source=source,
        target=carried,
        authored_by=authored_by,
        artifact_store=artifact_store,
    )
    return DuplicatedCourse(
        course=carried, tasks=copied, images=images_copied, skipped=tuple(skipped)
    )


def _copy_tasks(
    database: Database,
    *,
    source: Course,
    target: Course,
    authored_by: UserId,
    artifact_store: object | None,
) -> tuple[int, int, list[tuple[str, str]]]:
    """承認済みの課題を複製先に作り直す。"""
    skipped: list[tuple[str, str]] = []
    specs: list[TaskSpec] = []
    image_names: set[str] = set()

    with database.unit_of_work() as uow:
        for task in uow.tasks.list_for_course(source.id):
            # **写すのは出題されている版である。** 訂正の途中（承認待ちの
            # 新しい版がある）課題では、学習者に出ているのは 1 つ前の承認済み
            # の版で、そちらが「この課題」である ── 承認待ちの中身を新しい
            # 学期の初期値にすると、元コースで出したことのない問題が出る。
            version = uow.tasks.latest_published_version(task.id)
            if version is None:
                # 未承認は写さない（教員の指定）。下書きは「まだ出すと決めて
                # いないもの」で、新しい学期に自動で持ち込む理由がない。
                reason = "未承認です" if uow.tasks.latest_version(task.id) else "版がありません"
                skipped.append((task.title, reason))
                continue
            if not version.source_key:
                # 鍵が無い版は宣言に戻せない（鍵は課題の同一性の素材）。
                skipped.append((task.title, "課題の鍵が記録されていません"))
                continue

            # **正準キーは版に入っていない。** Q-matrix が持つのは KC の ID で、
            # ID はキーから導かれる一方向の値なので（`kc_id_for`）、体系から
            # 引き直す。引けない KC があれば**その課題は写さない** ── 黙って
            # 落とすと、複製先の課題は同じ問いなのに Q-matrix だけが欠けた
            # ものになり、習熟度が積まれない理由が画面から読めない（P6）。
            keys, missing = _kc_keys(uow, version)
            if missing:
                skipped.append((task.title, "知識要素が体系にありません"))
                continue

            statement, names = _restate(version.statement, source.id, target.id)
            image_names |= names
            specs.append(
                TaskSpec(
                    key=version.source_key,
                    statement=statement,
                    title=task.title,
                    unit=task.unit,
                    session=task.session,
                    position=task.position,
                    # **日程は写さない**（教員の指定）。写すと新学期の初日に
                    # 全課題が締切済みになる。
                    opens_at=None,
                    due_at=None,
                    max_score=version.max_score,
                    aggregation=version.aggregation,
                    reference_solution=version.reference_solution,
                    test_cases=_test_cases(version),
                    criteria=rubric.from_criteria(version.criteria),
                    knowledge_components=keys,
                )
            )

    for spec in specs:
        save_task(
            database,
            course_id=target.id,
            spec=spec,
            subject_profile=target.subject_profile,
            authored_by=authored_by,
            # **承認状態は引き継ぐ**（元は同じ教員のコースで、中身は承認済み）。
            # 写すのは承認済みだけなので、ここは定数でよい。
            review_state=ReviewState.APPROVED,
        )

    copied_images = _copy_images(artifact_store, source.id, target.id, sorted(image_names))
    return len(specs), copied_images, skipped


def _kc_keys(uow, version) -> tuple[tuple[str, ...], bool]:
    """版の Q-matrix から正準キーを引き直す。引けないものがあれば `True` を返す。"""
    keys: list[str] = []
    for entry in version.q_matrix:
        kc = uow.skills.get_kc(entry.kc_id)
        if kc is None:
            return (), True
        keys.append(kc.key)
    return tuple(keys), False


def _test_cases(version) -> tuple[dict, ...]:
    """保存済みのテストケースを宣言に戻す。

    `payload` のキー名は評価器が読むものと一致していなければならない
    （`spec.py` ── 違う名前だと全ケースが黙って不合格になる）。
    """
    return tuple(
        {
            "name": case.name,
            "input": str(case.payload.get("input", "")),
            "expected": str(case.payload.get("expected", "")),
            "hidden": case.hidden,
            "weight": case.weight,
        }
        for case in version.test_cases
    )


def _restate(statement: str, source_id: CourseId, target_id: CourseId) -> tuple[str, set[str]]:
    """課題文の画像 URL を複製先に向け直し、出てきた画像の名前を返す。

    URL の形を知っているのは `images.url_for` 1 か所である（#161）。ここで
    組み立て直すのは、その 1 か所を使って**元の形を作り、置き換える**ため。
    """
    prefix = statement_images.url_for(str(source_id), "")
    if prefix not in statement:
        return statement, set()
    names: set[str] = set()
    for fragment in statement.split(prefix)[1:]:
        name = fragment.split(")")[0].split('"')[0].split()[0] if fragment else ""
        if name:
            names.add(name)
    return statement.replace(prefix, statement_images.url_for(str(target_id), "")), names


def _copy_images(
    store: object | None, source_id: CourseId, target_id: CourseId, names: list[str]
) -> int:
    """課題文の画像を複製先の鍵へ写す。**写せないストアでは何もしない。**

    鍵は中身から導かれる（`images.new_name`）ので、同じ画像は同じ名前に
    なる ── 複製を二度流しても増えない。
    """
    if store is None or not names:
        return 0
    copied = 0
    for name in names:
        try:
            payload = store.get(statement_images.storage_key(str(source_id), name))  # type: ignore[attr-defined]
            store.put(  # type: ignore[attr-defined]
                statement_images.storage_key(str(target_id), name), payload
            )
        except Exception:  # pragma: no cover - ストアの実装差を吸収する
            # 画像 1 枚のために複製全体を巻き戻さない。課題文は写っており、
            # 欠けているのはその画像だけである。
            continue
        copied += 1
    return copied


__all__ = ["DuplicatedCourse", "duplicate_course"]
