"""知識要素（KC）の体系を保つ操作。

**教員が KC を足せること自体は正しい。** 科目の専門家はそこにしかいない。
荒れるのは、システムが「新規追加」と「打ち間違い」を区別できないからで、
`cs.loop.termination` と `cs.loops.termination` が静かに別物になる。だから
禁止ではなく**追加を明示的な行為にする**。禁止すると教員は既存の近いキーに
無理やり寄せ、構造としてはより悪くなる。

規則は 4 つ（`aijudge_core.knowledge` の docstring と対）。

  1. namespace は科目プロファイルが宣言したものだけ（`kc_namespaces`）。
     ブラウザから namespace を作れるようにした瞬間に `cs` と `csci` の
     分裂が起きる。プロファイルはコードと同じレビューを通る（ADR 0002）
  2. 新しい KC は**既存 KC の子**としてのみ足せる。孤立キーの山ではなく
     木を保つ。第 1 階層を作るのは稀で意図的な操作として区別する
  3. **改名しない。** ID はキーから導かれ Q-matrix は追記のみ（P8）。
     誤りは `deprecated` にして `superseded_by` で後継を指す
  4. AI には KC を作らせない。生成は登録済みからの選択だけ

**KC はコースをまたいで共有される。** 同じ namespace を使うコースは同じ
語彙を見る（それが設計原則 P6 の狙いで、習熟度が学期をまたいで積み上がる
のもこの性質による）。だから 1 コースの中で完結する操作ではない ── 誰が
いつ足したかを残し、どれだけ使われているかを見せる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aijudge_core import KnowledgeComponent, kc_id_for, parse_kc_key
from aijudge_core.ids import KcId, UserId
from aijudge_persistence import Database

from .finalization import _courses as _all_course_rows
from .operations import AdminError


def _all_courses(database: Database):
    """全テナントの全コース。**KC はコースをまたいで共有される**ので、

    利用状況も 1 コースに閉じては数えられない。
    """
    return _all_course_rows(database, None)


@dataclass(frozen=True)
class KcUsage:
    """1 つの KC がどれだけ使われているか。

    引退させてよいかの判断材料。**コースをまたいで数える** ── 自分の
    コースで使っていなくても、他のコースが使っていれば影響がある。
    """

    kc: KnowledgeComponent
    tasks: int = 0
    courses: int = 0

    @property
    def used(self) -> bool:
        return self.tasks > 0


def allowed_namespaces(profile) -> tuple[str, ...]:
    """この科目プロファイルが使ってよい名前空間。

    宣言が無いプロファイルは KC を扱わない（Phase 4 前の科目がそれ）。
    """
    return tuple(profile.kc_namespaces)


def list_for_namespaces(
    database: Database, namespaces: tuple[str, ...], *, include_deprecated: bool = True
) -> tuple[KnowledgeComponent, ...]:
    """指定した名前空間の KC を、キー順にすべて返す。"""
    found: list[KnowledgeComponent] = []
    with database.unit_of_work() as uow:
        for namespace in namespaces:
            found.extend(uow.skills.list_kcs(namespace))
    if not include_deprecated:
        found = [kc for kc in found if not kc.deprecated]
    return tuple(sorted(found, key=lambda kc: kc.key))


def assert_registered(
    database: Database,
    keys: tuple[str, ...],
    *,
    course_keys: tuple[str, ...] = (),
) -> None:
    """課題が名指しした KC が、登録済みで、このコースが使う範囲にあることを確かめる。

    **ここが「登録してから使う」を強制する唯一の場所。** 模型の層
    （`q_matrix_for`）は保存先を知らないので確かめられない。

    `course_keys` はコースが宣言した範囲（`Course.knowledge_components`）。
    **空なら名前空間の全部**として扱う ── 宣言していないコースの取り込みを、
    この検証が壊さないため（後方互換の既定）。

    **画面で絞るだけにしない。** 作問フォームの候補を絞っても、API 経由の
    投入（`aijudge_reviewconsole.api`）が同じ経路を通る。UI で隠すのは
    表示の都合であって制限ではない。
    """
    if not keys:
        return
    missing: list[str] = []
    with database.unit_of_work() as uow:
        for key in keys:
            if uow.skills.get_kc(kc_id_for(key)) is None:
                missing.append(key)
    if missing:
        raise AdminError(
            "登録されていない知識要素です: "
            + ", ".join(sorted(missing))
            + "（先に体系へ追加してください）"
        )

    if not course_keys:
        return
    outside = sorted(set(keys) - set(course_keys))
    if outside:
        raise AdminError(
            "このコースが使う知識要素に含まれていません: "
            + ", ".join(outside)
            + "（知識要素のページで「このコースで使う」に入れてください）"
        )


def register(
    database: Database,
    *,
    key: str,
    label: str,
    description: str | None = None,
    namespaces: tuple[str, ...],
    actor_id: UserId | None = None,
    allow_root: bool = False,
    now: datetime | None = None,
) -> KnowledgeComponent:
    """KC を 1 つ足す。既にあればそれを返す（何度押しても増えない）。

    `namespaces` は足してよい名前空間（科目プロファイルの宣言）。
    `allow_root` は第 1 階層を作る許可で、既定では出さない ── 新しい分野の
    根を作るのは管理者の操作である。
    """
    try:
        namespace, path = parse_kc_key(key)
    except ValueError as exc:
        raise AdminError(str(exc)) from None

    if namespace not in namespaces:
        raise AdminError(
            f"名前空間 {namespace!r} はこの科目では使えません"
            f"（使えるのは {', '.join(namespaces) or 'なし'}）。"
            "名前空間を増やすには科目プロファイルの変更が要ります。"
        )

    existing_id = kc_id_for(key)
    with database.unit_of_work() as uow:
        existing = uow.skills.get_kc(existing_id)
        if existing is not None:
            return existing

        parent_id: KcId | None = None
        if len(path) > 1:
            parent_key = ".".join((namespace, *path[:-1]))
            parent = uow.skills.get_kc(kc_id_for(parent_key))
            if parent is None:
                # **孤立キーを作らせない。** 親から作らせることで、体系が
                # 平らなキーの山ではなく木のまま保たれる。
                raise AdminError(
                    f"親の知識要素 {parent_key!r} がまだありません。先にそちらを追加してください。"
                )
            parent_id = parent.id
        elif not allow_root:
            # **名前空間は階層に数えない、と明示する。** `cs.c_language` は
            # 点が 2 つに分かれて見えるので、これを「第 1 階層」とだけ言うと
            # 何を指しているのか読めない（実際に読めなかった）。どこまでが
            # 名前空間で、どう書けば子になるのかを、そのキーで示す。
            raise AdminError(
                f"{key!r} は名前空間 {namespace!r} の第 1 階層（分野の根）です。"
                f"**名前空間 {namespace!r} は階層に数えません。** "
                f"この下に子を足す形（{key}.… ）なら第 2 階層で、教員が追加できます。"
                "新しい分野の根を作るには管理者の操作が要ります。"
            )

        kc = KnowledgeComponent(
            id=existing_id,
            namespace=namespace,
            path=path,
            label=label.strip() or key,
            description=(description or "").strip() or None,
            parent_id=parent_id,
            created_by=actor_id,
            created_at=now or datetime.now(UTC),
        )
        uow.skills.save_kc(kc)
        uow.commit()
    return kc


def retire(
    database: Database,
    *,
    key: str,
    superseded_by_key: str | None = None,
) -> KnowledgeComponent:
    """KC を引退させる。**消さない。**

    ID はキーから導かれ、Q-matrix は追記のみ（P8）。消すと過去の課題が
    何を問うていたのか辿れなくなる。後継を指すと、以後の作問と表示は
    そちらへ寄せられる。
    """
    with database.unit_of_work() as uow:
        kc = uow.skills.get_kc(kc_id_for(key))
        if kc is None:
            raise AdminError(f"知識要素 {key!r} がありません")
        successor: KcId | None = None
        if superseded_by_key:
            if superseded_by_key == key:
                raise AdminError("自分自身を後継にはできません")
            target = uow.skills.get_kc(kc_id_for(superseded_by_key))
            if target is None:
                raise AdminError(f"後継の知識要素 {superseded_by_key!r} がありません")
            if target.deprecated:
                # 引退した KC を後継にすると、辿った先がまた引退している。
                raise AdminError(f"{superseded_by_key!r} は引退済みです")
            successor = target.id
        retired = kc.model_copy(update={"deprecated": True, "superseded_by": successor})
        uow.skills.save_kc(retired)
        uow.commit()
    return retired


def edit(
    database: Database,
    *,
    key: str,
    label: str,
    description: str | None = None,
) -> KnowledgeComponent:
    """名前と説明を直す。**キーは直せない。**

    規則 3（改名しない）が守っているのは**キー**である ── `KcId` はキーから
    導かれ（`kc_id_for`）、Q-matrix は追記のみ（P8）。キーを変えることは別の
    知識要素を作ることで、過去の課題が何を問うていたのか辿れなくなる。

    **`label` と `description` はそこに関わらない。** ID もキーも Q-matrix も
    動かないので、変えても過去の採点がどの知識要素を指していたかは変わらない。
    打ち間違えた名前を直すために引退や削除を使うのは、同一性を壊す操作を
    表示の都合で持ち出すことになる。
    """
    with database.unit_of_work() as uow:
        kc = uow.skills.get_kc(kc_id_for(key))
        if kc is None:
            raise AdminError(f"知識要素 {key!r} がありません")
        if not label.strip():
            # 名前を空にすると一覧がキーだけになる。キーは人が読む名前ではない。
            raise AdminError("名前を空にはできません")
        updated = kc.model_copy(
            update={
                "label": label.strip(),
                "description": (description or "").strip() or None,
            }
        )
        uow.skills.save_kc(updated)
        uow.commit()
    return updated


def delete(database: Database, *, key: str) -> KnowledgeComponent:
    """**一度も使われていない KC だけを消す。** 使われていれば消さない。

    「消さない」（`retire`）は**使われた KC の話**である ── Q-matrix が
    指しているものを消すと、過去の課題が何を問うていたのか辿れなくなり、
    その課題で付いた習熟度の出所も失われる（P8）。

    一度も使われていない KC には、その履歴が無い。打ち間違えた根
    （`cs.c_langauge`）を引退させて残すと、**コースをまたいで共有される
    一覧に、誰の役にも立たない行が永久に並ぶ**。引退は「使っていたが今後は
    使わない」を表す記録であって、打ち間違いの置き場所ではない。

    子を持つ KC も消さない。親を消すと子の `parent_id` が宙に浮き、木が
    壊れる（孤立キーを作らせない、という規則 2 の裏側）。子から先に消す。
    """
    with database.unit_of_work() as uow:
        kc = uow.skills.get_kc(kc_id_for(key))
        if kc is None:
            raise AdminError(f"知識要素 {key!r} がありません")

    counted = usage(database, (kc,))[kc.key]
    if counted.used:
        raise AdminError(
            f"{key!r} は課題 {counted.tasks} 件・コース {counted.courses} 件で使われています。"
            "消すと、その課題が何を問うていたのか辿れなくなります。"
            "使わなくするだけなら引退させてください。"
        )

    children = [
        other
        for other in list_for_namespaces(database, (kc.namespace,))
        if other.parent_id == kc.id
    ]
    if children:
        raise AdminError(
            f"{key!r} には子の知識要素があります（"
            + ", ".join(sorted(child.key for child in children))
            + "）。先に子を消してください。"
        )

    with database.unit_of_work() as uow:
        uow.skills.delete_kc(kc.id)
        uow.commit()
    return kc


def restore(database: Database, *, key: str) -> KnowledgeComponent:
    """引退を取り消す。押し間違いを直すための操作。"""
    with database.unit_of_work() as uow:
        kc = uow.skills.get_kc(kc_id_for(key))
        if kc is None:
            raise AdminError(f"知識要素 {key!r} がありません")
        revived = kc.model_copy(update={"deprecated": False, "superseded_by": None})
        uow.skills.save_kc(revived)
        uow.commit()
    return revived


def usage(database: Database, kcs: tuple[KnowledgeComponent, ...]) -> dict[str, KcUsage]:
    """KC ごとの利用状況。**コースをまたいで数える。**

    課題版の Q-matrix を 1 度だけ走査する。KC ごとに問い合わせると
    体系の大きさ × 課題数になる。
    """
    counts: dict[str, set[str]] = {}
    tasks: dict[str, int] = {}
    courses = _all_courses(database)
    with database.unit_of_work() as uow:
        for course_id, version in _versions_with_course(uow, courses):
            for entry in version.q_matrix:
                key = str(entry.kc_id)
                tasks[key] = tasks.get(key, 0) + 1
                counts.setdefault(key, set()).add(str(course_id))
    return {
        kc.key: KcUsage(
            kc=kc,
            tasks=tasks.get(str(kc.id), 0),
            courses=len(counts.get(str(kc.id), ())),
        )
        for kc in kcs
    }


def _versions_with_course(uow, courses):
    """全コースの最新課題版を（コース ID とともに）返す。"""
    for course in courses:
        for task in uow.tasks.list_for_course(course.id):
            version = uow.tasks.latest_version(task.id)
            if version is not None:
                yield course.id, version


__all__ = [
    "KcUsage",
    "allowed_namespaces",
    "assert_registered",
    "delete",
    "edit",
    "list_for_namespaces",
    "register",
    "restore",
    "retire",
    "usage",
]
