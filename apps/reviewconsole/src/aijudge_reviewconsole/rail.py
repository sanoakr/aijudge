"""左の常設の帯（#189・ADR 0017）。

コースの中には行き先が 13 ある。どれもコースのメニュー（`/courses/{id}`）から
しか辿れず、いまどこに居るかを示すのはパンくずだけだった ── 「提出の一覧」から
「確定処理」へ移るのに一段戻って下りることになり、担当が 4 コースあれば週に
何十回もそれをする。**件数はコースのメニューにしか無い**ので、どこに用が
あるかは開くまで分からない。

**帯は 2 つの形を持つ**（ADR 0017 §1）。コース単位（20 経路）とテナント単位
（14 経路）で、後者は「データが無いコースの帯」ではなく別の帯である ──
利用者の一覧に空のコース帯を出すのは、情報が無いことを情報のように見せる。

ここが持つのは**組み立てだけ**。描き方は `packages/webui` の `_rail.html` が
持ち、配るのは `context.py` の context processor が持つ。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aijudge_core import Course

__all__ = ["Rail", "RailGroup", "RailItem", "course_rail", "tenant_rail"]


@dataclass(frozen=True)
class RailItem:
    """行き先 1 つ。

    `count` は `None` なら数字を出さない ── **数えられないことと 0 件は
    別のこと**。**0 件も数字は出さない**（2026-09-25）── 「0」の丸が並ぶと、
    用があるように見える（利用者の指摘）。数えられなかったことは帯の下の
    断り書き（`counts_unavailable`）が言う。
    `attention` は「人が押さないと先へ進まない」の琥珀（`base.css` の規則）。
    `note` は数字の補足（例: 確定処理の「自動確定待ち 12」）。
    """

    label: str
    href: str
    count: int | None = None
    attention: bool = False
    note: str = ""
    #: いまこの行き先を見ているか。**照合はパスで行う**（`rail_context`）──
    #: 画面ごとに鍵を書く取り決めにすると、書き忘れた画面だけ現在地が
    #: 出ない、という気づきにくい壊れ方をする。
    current: bool = False


@dataclass(frozen=True)
class RailGroup:
    """大項目 1 つと、その下の行き先。

    **組にする**のが要点（#189）。以前は「採点」「出題」「設定」が小さな
    字のラベルとして行き先と同じ列に並んでいたので、見出しなのか押せるのか、
    どこまでがその下なのかが読めなかった。
    """

    title: str
    items: tuple[RailItem, ...]
    meta: str = ""


@dataclass(frozen=True)
class Rail:
    """帯 1 つ。

    **コースの切り替えは持たない。** モックでは帯の頭に置いたが、切り替えの
    経路がまだ無く、動かない `select` を出すことになる ── 押しても何も
    起きない部品を見せないのがこの画面群の作法である。担当コースへは
    帯の銘（`href`）から一段で戻れる。
    """

    title: str
    subtitle: str = ""
    groups: tuple[RailGroup, ...] = ()
    href: str = "/"
    #: 数えられなかったか（ADR 0017 §3）。行き先は出すが数字は出さない。
    counts_unavailable: bool = False
    extras: dict[str, str] = field(default_factory=dict)


def course_rail(
    course: Course,
    *,
    contested: int | None,
    unfinalized: int | None,
    can_manage: bool,
    finalize_waiting: int | None = None,
) -> Rail:
    """コース単位の帯。

    **載せるのは「人が動かないと進まないもの」だけ**（ADR 0017 §4）。
    提出の総数・受講者数・知識要素の数は載せない ── どこに用があるかを
    教えないので、全ページがクエリを払う理由にならない。行き先は数字が
    無くても行き先である。

    `can_manage` が偽（TA）のときは、押すと 403 になる行き先を出さない ──
    **見えるのに押せないものを並べない**（#102 で同じことをした）。
    """
    base = f"/courses/{course.id}"
    manage = f"/manage/courses/{course.id}"

    grading = RailGroup(
        title="採点",
        items=(
            RailItem("提出", f"{base}/submissions"),
            RailItem(
                "再確認の依頼",
                f"{base}/queue",
                count=contested,
                attention=bool(contested),
            ),
            # **手動の確定が要るものだけを数字にする**（2026-09-25）。自動確定を待って
            # いるものは放っておけば閉じるので、数字に混ぜると用があるように見える。
            # 待っている件数は補足に書く（`unfinalized` は呼ぶ側で手動の分だけにしてある）。
            RailItem(
                "確定処理",
                f"{base}/finalize",
                count=unfinalized,
                attention=bool(unfinalized),
                note=f"自動確定待ち {finalize_waiting}" if finalize_waiting else "",
            ),
            RailItem("blind 採点", f"{base}/blind"),
        ),
    )
    # **項目名は行き先の見出しと同じ語にする**（#300）。違う語を当てると、
    # 押した先が目当ての画面かどうかを見出しで確かめられない ── 「問題セット」
    # がコース全体の設定へ飛んでいたのがその形だった。
    #
    # 問題セットは TA にも出す（#102）── 採点している課題を読めないと、
    # 学習者の質問にも自分の付けた点にも答えられない。行き先はコースの
    # 一覧（`/courses/{id}`）そのもので、追加と日程はその先にある。
    authoring = [RailItem("問題セット", base)]
    if can_manage:
        # ブラウザのエディタでの作業の記録（ADR 0023）。**教員だけ** ── TA には
        # 開けない画面なので出さない。提出していない学習者の記録にも、ここから辿れる。
        grading = RailGroup(
            title=grading.title,
            items=(*grading.items, RailItem("作業の記録", f"{base}/activity")),
        )
    groups = [grading]
    if can_manage:
        # 科目・ルーブリック・提出形式など、**このコースの課題すべてに効く値**。
        # 課題ごとの設定は課題のページが持つ。
        authoring.append(RailItem("共通設定", manage))
        authoring.append(RailItem("未承認の課題（AI 作問）", f"{manage}/drafts"))
    groups.append(RailGroup(title="出題", items=tuple(authoring)))
    if can_manage:
        groups.append(
            RailGroup(
                title="設定",
                items=(
                    RailItem("コースの基本情報", f"{manage}/basics"),
                    RailItem("受講者", f"{manage}/enrolments"),
                    RailItem("知識要素（KC）", f"{manage}/kc"),
                    # 習熟度（#328）。**知識要素の隣に置く** ── 何を問うかを
                    # 決める画面と、その結果どうなっているかを見る画面である。
                    # TA には出さない（`_require_enrolment_manager` と揃える）。
                    RailItem("習熟度", f"{manage}/mastery"),
                ),
            )
        )
    return Rail(
        title=course.title,
        subtitle=f"{course.code} · {course.term}",
        groups=tuple(groups),
        href=base,
        counts_unavailable=contested is None and unfinalized is None,
    )


def tenant_rail(*, is_admin: bool) -> Rail:
    """テナント単位の帯。**コースを持たない画面のためのもの**（ADR 0017 §1）。

    利用者の一覧・科目プロファイル・ログイン方式はテナント単位で、コースが
    無い。ここに空のコース帯を出すと、情報が無いことを情報のように見せる。

    管理者でなければ行き先は「担当コース」だけになる ── それでも帯は出す。
    **出さないと、コースの画面から出た瞬間に帯が消える**（画面の骨格が
    ページによって変わるのは、それ自体が迷いの元である）。
    """
    items = [RailItem("担当コース", "/")]
    groups = [RailGroup(title="コース", items=tuple(items))]
    if is_admin:
        groups.append(
            RailGroup(
                title="テナントの設定",
                # 項目名は行き先の見出しと同じ語で（#300）。
                items=(
                    RailItem("利用者の一覧", "/manage/users"),
                    RailItem("科目プロファイル", "/manage/subjects"),
                    RailItem("Google ログイン設定", "/manage/oidc-settings"),
                    # 学内ネットワーク（#333）。**ログイン設定の隣** ── どちらも
                    # 「誰が入れるか」をテナント単位で決める設定である。
                    RailItem("学内ネットワーク", "/manage/campus-networks"),
                ),
            )
        )
    return Rail(title="aiJudge", subtitle="教員コンソール", groups=tuple(groups))
