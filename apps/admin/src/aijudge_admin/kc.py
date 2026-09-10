"""知識要素（KC）の体系を保つ操作。

**教員が KC を足せること自体は正しい。** 科目の専門家はそこにしかいない。
荒れるのは、システムが「新規追加」と「打ち間違い」を区別できないからで、
`cs.loop.termination` と `cs.loops.termination` が静かに別物になる。だから
禁止ではなく**追加を明示的な行為にする**。禁止すると教員は既存の近いキーに
無理やり寄せ、構造としてはより悪くなる。

規則は 5 つ（`aijudge_core.knowledge` の docstring と対）。

  1. namespace は科目プロファイルが宣言したものだけ（`kc_namespaces`）。
     ブラウザから namespace を作れるようにした瞬間に `cs` と `csci` の
     分裂が起きる。プロファイルはコードと同じレビューを通る（ADR 0002）
  2. **分野（第 1 階層）と単位（第 2 階層）は骨格が決める。**
     `subjects/kc/*.yaml` にあるものだけで、画面からは足せない ── CS2023 の
     Knowledge Area / Knowledge Unit をそのまま使っており、ここを開けると
     `cs.loops` と `cs.iteration` が並ぶ。増やすのは骨格ファイルの変更、
     つまりコードと同じレビューを通る決定である
  3. **知識要素（第 3 階層）は教員が足せる。深さはそこまで。**
     骨格に入っているのは推奨候補であって正解の一覧ではない。足すときは
     **近いものが提示される**（禁止ではなく提示 ── 禁止すると教員は近い
     キーに無理やり寄せ、構造としてはより悪くなる）。第 4 階層は作れない:
     細かくしすぎると 1 つの KC に課題が 1 件しか対応せず、習熟度が
     推定できない（Q-matrix が薄くなる）
  4. **改名しない。** ID はキーから導かれ Q-matrix は追記のみ（P8）。
     誤りは `deprecated` にして `superseded_by` で後継を指す
  5. AI が新しい知識要素を**推薦**することはできる。ただし作るのは人で、
     推薦は必ず既存の単位の下に付き、近いものが併記される

**KC はコースをまたいで共有される。** 同じ namespace を使うコースは同じ
語彙を見る（それが設計原則 P6 の狙いで、習熟度が学期をまたいで積み上がる
のもこの性質による）。だから 1 コースの中で完結する操作ではない ── 誰が
いつ足したかを残し、どれだけ使われているかを見せる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aijudge_authoring.similarity import lexical, overlap
from aijudge_core import KnowledgeComponent, kc_id_for, parse_kc_key
from aijudge_core.ids import KcId, UserId
from aijudge_persistence import Database

from .finalization import _courses as _all_course_rows
from .kc_skeleton import MAX_KC_DEPTH
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
    seeding: bool = False,
    now: datetime | None = None,
) -> KnowledgeComponent:
    """KC を 1 つ足す。既にあればそれを返す（何度押しても増えない）。

    `namespaces` は足してよい名前空間（科目プロファイルの宣言）。

    **`seeding` は骨格の投入だけが立てる。** 分野（第 1 階層）と単位
    （第 2 階層）を作れるのはこの経路だけで、画面からは知識要素
    （第 3 階層）しか足せない ── 管理者であっても同じである。管理者に
    開けても構わないように見えるが、**分野を増やすのは「その場の判断」に
    してはいけない決定**で、骨格ファイルを直してレビューを通す方に寄せる
    （`kc_skeleton.py` 冒頭）。
    """
    try:
        namespace, path = parse_kc_key(key)
    except ValueError as exc:
        # **形の話を日本語で言い切る。** コアの例外は `invalid KC path
        # segment: '配列'` のような英語の一行で、これを画面にそのまま出すと
        # 「何が悪いのか」は伝わっても「何なら通るのか」が伝わらない。
        # 日本語のキーを書いてしまう人にとって、そこが唯一知りたいこと（#157）。
        raise AdminError(
            f"知識要素のキー {key!r} は使えません（{exc}）。"
            "キーは `名前空間.親.子` の形で、半角英小文字・数字・下線だけを使います"
            "（例 `cs.loops.termination`）。日本語は名前・説明のほうに書いてください。"
        ) from None

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
        if len(path) > MAX_KC_DEPTH and not seeding:
            # **深さは 3 で止める。** 細かくしすぎると 1 つの KC に課題が
            # 1 件しか対応せず、習熟度が推定できない（Q-matrix が薄くなる）。
            raise AdminError(
                f"{key!r} は第 {len(path)} 階層です。知識要素は"
                f"「分野.単位.知識要素」の第 {MAX_KC_DEPTH} 階層までにしてください"
                f"（例 `cs.sdf.fundamentals.branching`）。"
            )
        if len(path) > 1:
            parent_key = ".".join((namespace, *path[:-1]))
            parent = uow.skills.get_kc(kc_id_for(parent_key))
            if parent is None:
                # **孤立キーを作らせない。** 親から作らせることで、体系が
                # 平らなキーの山ではなく木のまま保たれる。
                if not seeding:
                    # **「先に追加してください」と言わない。** 分野も単位も
                    # 画面からは足せないので、そう案内すると教員は足せない
                    # ものを足そうとして二度断られる。
                    raise AdminError(
                        f"{'分野' if len(path) == 2 else '単位'} {parent_key!r} が"
                        "骨格にありません。分野と単位は `subjects/kc/` の骨格ファイル"
                        "（CS2023 の Knowledge Area / Knowledge Unit）で決まっており、"
                        "画面からは足せません。既にある単位の下に足してください。"
                    )
                raise AdminError(
                    f"親の知識要素 {parent_key!r} がまだありません。先にそちらを追加してください。"
                )
            parent_id = parent.id
            if len(path) == 2 and not seeding:
                # **単位（第 2 階層）は骨格が決める。** ここを開けると
                # `cs.sdf.loops` と `cs.sdf.iteration` が並ぶ。
                raise AdminError(
                    f"{key!r} は単位（第 2 階層）です。"
                    "分野と単位は `subjects/kc/` の骨格ファイル（CS2023 の Knowledge Area / "
                    "Knowledge Unit）で決まっており、画面からは足せません。"
                    f"この下に知識要素を足す形（{key}.… ）なら追加できます。"
                )
        elif not seeding:
            # **名前空間は階層に数えない、と明示する。** `cs.c_language` は
            # 点が 2 つに分かれて見えるので、これを「第 1 階層」とだけ言うと
            # 何を指しているのか読めない（実際に読めなかった）。どこまでが
            # 名前空間で、どう書けば子になるのかを、そのキーで示す。
            raise AdminError(
                f"{key!r} は分野（第 1 階層）です。"
                f"**名前空間 {namespace!r} は階層に数えません。** "
                "分野と単位は `subjects/kc/` の骨格ファイル（CS2023 の Knowledge Area / "
                "Knowledge Unit）で決まっており、画面からは足せません。"
                f"知識要素を足すなら `{key}.単位.名前` の形になります。"
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


# -- 近いものを見せる ------------------------------------------------------
#
# **禁止ではなく提示。** 同じ概念に別のキーを作らせないための仕掛けだが、
# 一致を強制すると、教員は近いだけの枝に無理やり寄せる ── それは体系が
# 整ったように見えて、実際には嘘の対応づけが 1 件増えただけである。
#
# **分野と単位をまたいで探す。** 「配列」は `cs.sdf.data_structures` にも
# `cs.al.foundational` にもあり、教員が今いる単位の外に正解があることは
# 普通に起きる。同じ単位の中だけ見ると、いちばん見つけたい重複を見逃す。

# これを下回る類似は出さない。字面の 3-gram Jaccard で 0.4 は「かなり似ている」。
SUGGESTION_THRESHOLD = 0.4
MAX_SUGGESTIONS = 5


@dataclass(frozen=True)
class KcSuggestion:
    """近い既存 KC 1 件。"""

    kc: KnowledgeComponent
    score: float
    # 教員がいま足そうとしている単位と同じ枝か。**違う枝のほうが重要**
    # ── 同じ枝の重複は目で見つかるが、別の分野にある同義語は見つからない。
    same_unit: bool

    @property
    def key(self) -> str:
        return self.kc.key


def suggest_similar(
    database: Database,
    *,
    key: str,
    label: str,
    description: str | None = None,
    namespaces: tuple[str, ...],
    threshold: float = SUGGESTION_THRESHOLD,
    limit: int = MAX_SUGGESTIONS,
    existing: tuple[KnowledgeComponent, ...] | None = None,
) -> tuple[KcSuggestion, ...]:
    """足そうとしている KC に近い既存 KC を、近い順に返す。

    **字面で測る。** 埋め込みを使えば言い換えも拾えるが、S6 が止まっていても
    追加は通らなければならない ── 提示は補助であって関門ではないので、
    ここで LLM に依存させない（`duplicates.py` が「どちらで測ったか」を
    必ず言うのと同じ理由で、字面だけであることは画面に書く）。
    """
    # **ラベルとキーを混ぜて測らない。** 混ぜると、日本語のラベルと英語の
    # キーが同じ文字列の中に入り、`repetition` と `notion` が 3-gram を
    # 共有するだけで「近い」になる（実際になった）。言語の違う 2 つの軸は
    # 別々に測って、大きい方を採る。
    text = " ".join(part for part in (label, description or "") if part).strip()
    segment = key.split(".")[-1]
    if not text and not segment:
        return ()

    try:
        _, path = parse_kc_key(key)
    except ValueError:
        path = ()
    unit_key = ".".join(key.split(".")[:-1]) if len(path) >= 2 else None

    # 候補を 20 件まとめて調べるときに 20 回引き直さないよう、読み込み済みの
    # 一覧を渡せるようにしてある。
    pool = (
        tuple(kc for kc in existing if not kc.deprecated)
        if existing is not None
        else list_for_namespaces(database, namespaces, include_deprecated=False)
    )
    scored: list[KcSuggestion] = []
    for kc in pool:
        if kc.key == key:
            continue
        # 分野と単位そのものは提案しない。足せるのは知識要素だけなので、
        # 「これに寄せては」と言われても寄せようがない。
        if len(kc.path) < MAX_KC_DEPTH:
            continue
        other = " ".join(part for part in (kc.label, kc.description or "") if part).strip()
        score = max(
            # ラベルは短い日本語。片方がもう片方を含む形（「くりかえし」と
            # 「回数の決まったくりかえし」）は同じ概念なので、包含を採る。
            _closeness(text, other, containment=True),
            # キーは英語の識別子。短い語が偶然 3-gram を共有するので、
            # 包含は採らない（`loop` が `event_loop` に一致してしまう）。
            _closeness(segment, kc.path[-1], containment=False),
        )
        if score >= threshold:
            scored.append(
                KcSuggestion(
                    kc=kc,
                    score=score,
                    same_unit=unit_key is not None and kc.key.rsplit(".", 1)[0] == unit_key,
                )
            )
    scored.sort(key=lambda s: (-s.score, s.key))
    return tuple(scored[:limit])


# -- 骨格の投入 ------------------------------------------------------------


@dataclass(frozen=True)
class SeedReport:
    """投入の結果。**足した数と、既にあった数を分けて返す。**

    「17 件投入しました」だけだと、2 度目に走らせた運用者は何が起きたのか
    分からない（何度走らせても増えないのが正しい振る舞いである）。
    """

    namespace: str
    source: str
    added: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.added) + len(self.existing)

    def summary(self) -> str:
        lines = [
            f"名前空間 {self.namespace}: {self.total} 件（新規 {len(self.added)} / "
            f"既存 {len(self.existing)}）"
        ]
        if self.source:
            lines.append(f"  出典: {self.source}")
        return "\n".join(lines)


def seed(
    database: Database,
    skeleton,
    *,
    namespaces: tuple[str, ...],
    now: datetime | None = None,
) -> SeedReport:
    """骨格を投入する。**何度走らせても増えない。**

    `created_by` は付けない ── 骨格は誰か個人が足したものではなく、
    ファイルをレビューして決めたものである。誰が投入したかは監査ログに残る
    （ADR 0016）。

    **親から順に投入する。** `register` が親の不在で落ちるので、
    分野 → 単位 → 知識要素の順でなければ通らない。骨格ファイルの並び順に
    依存させず、深さで並べ替えてから入れる。
    """
    if skeleton.namespace not in namespaces:
        raise AdminError(
            f"名前空間 {skeleton.namespace!r} はこの科目では使えません"
            f"（使えるのは {', '.join(namespaces) or 'なし'}）。"
            "科目プロファイルの `kc_namespaces` を確認してください。"
        )

    added: list[str] = []
    existing: list[str] = []
    for entry in sorted(skeleton.entries, key=lambda e: (e.depth, e.key)):
        key = f"{skeleton.namespace}.{entry.key}"
        before = None
        with database.unit_of_work() as uow:
            before = uow.skills.get_kc(kc_id_for(key))
        register(
            database,
            key=key,
            label=entry.label,
            namespaces=namespaces,
            actor_id=None,
            seeding=True,
            now=now,
        )
        (existing if before is not None else added).append(key)
    return SeedReport(
        namespace=skeleton.namespace,
        source=skeleton.source,
        added=tuple(added),
        existing=tuple(existing),
    )


def _closeness(left: str, right: str, *, containment: bool) -> float:
    """近さ。`containment` を立てると、包含（Overlap 係数）も見る。

    Jaccard だけだと、短い語が長い語に含まれる場合を見落とす ──
    「くりかえし」と「回数の決まったくりかえし」は、共通部分が短い側を
    覆い尽くしているのに、和集合が大きいので値が伸びない。実際に見落とした。

    **ただし包含はどこでも正しいわけではない。** 英語の識別子どうしでは
    短い語が偶然 3-gram を共有し、`loop` が `event_loop` にも
    `game_loop` にも 1.0 で一致して、肝心の候補を押し出す。だから
    日本語のラベルにだけ許し、キーには許さない。

    課題の重複検査（`duplicates.py`）が包含を使わないのも同じ話で、
    **測り方は用途で変わる。**
    """
    if not left or not right:
        return 0.0
    score = lexical(left, right)
    return max(score, overlap(left, right)) if containment else score
