"""シラバスから、コース登録と知識要素の**候補**を作る。

**候補であって登録ではない。** 出てきたものを教員が選んで初めて実体になる
（KC を AI に作らせない、という規則はここでも同じ・`aijudge_admin.kc`）。
シラバスは書き方が科目ごとに違うので、機械が読んだ結果をそのまま体系に
入れると、粒度も語彙もばらばらの KC が並ぶ。

**本文はサーバが取りに行かない。** 龍谷大学のシラバスは JavaScript で
描画されるページで、URL を取得しても空の外枠しか返らない（実測 1815 バイト、
`<title>acslb-client</title>` だけ）。ヘッドレスブラウザを 1 台の運用に
持ち込む価値は無いので、**本文そのものを受け取る** ── 貼り付けか、
PDF / DOCX の添付。読めない URL を保存しても意味が無いので、URL は持たない。

PDF の抽出は採点側と同じもの（`aijudge_ext_document_text.text_of`）を使う。
別に実装すると、片方だけが壊れた PDF を読めるという差が出て、教員が
「なぜ読めないのか」を切り分けられなくなる。

外へ取りに行かないことには副次的な利点もある ── サーバが任意の URL を
取得する経路を作らずに済む（内部ネットワークへの踏み台にならない）。

**個人データを含まない**（`DataClass.NON_PERSONAL`）。シラバスは公開情報で、
学習者の解答も氏名も渡さない（設計原則 P7）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import is_valid_kc_key
from aijudge_llm_gateway import (
    DataClass,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)

# 龍谷大学のシラバスの deep link。`i` がシラバス管理番号、`n` が年度。
# 履修登録コードとは別物である。
SYLLABUS_URL_TEMPLATE = (
    "https://syllabus.ws.ryukoku.ac.jp/acrsw/CSylNoSSO/CNoSSO.do?i={code}&n={year}"
)
SYLLABUS_EXAMPLE = SYLLABUS_URL_TEMPLATE.format(code="Y001009010", year=2026)


# 添付できるシラバスの上限。シラバス 1 科目分に MB は要らない。
MAX_SYLLABUS_BYTES = 4 * 1024 * 1024

# 本文を取り出せる添付の形式。
DOCUMENT_SUFFIXES: dict[str, str] = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "txt"}


class SyllabusError(Exception):
    """シラバスを読めなかった。教員に理由を返せる形にする。"""


def deep_link(code: str, year: int | str) -> str:
    """シラバス管理番号と年度から deep link を組み立てる。"""
    return SYLLABUS_URL_TEMPLATE.format(code=code.strip(), year=year)


# 見出しに見える行。抽出した本文を Markdown に均すのに使う。**見出しの
# 付け方だけを直し、中身は触らない** ── 書き換えると、教員が「シラバスに
# こう書いてあったか」を確かめられなくなる。
_HEADINGS = (
    "科目名",
    "担当者",
    "担当教員",
    "開講",
    "単位",
    "授業のねらい",
    "講義概要",
    "到達目標",
    "授業計画",
    "成績評価",
    "評価方法",
    "教科書",
    "参考書",
    "履修上の注意",
)


def to_markdown(text: str) -> str:
    """抽出した本文を Markdown に均す。**best-effort。**

    シラバスの体裁は科目ごとに違うので、完全な変換は狙わない。見出しらしい
    行に `##` を付け、`第 N 回` を箇条書きにするところまで。読める形に
    なっていれば、あとは教員が直せる。
    """
    import re

    session = re.compile(r"^(第\s*\d+\s*回)[\s:：.．]*(.*)$")
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        matched = session.match(line)
        if matched is not None:
            body = matched.group(2).strip()
            lines.append(f"- **{matched.group(1)}** {body}".rstrip())
            continue
        head = line.rstrip("：: ")
        if head in _HEADINGS or (len(head) <= 12 and any(head.startswith(h) for h in _HEADINGS)):
            lines.append("")
            lines.append(f"## {head}")
            continue
        lines.append(line)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def read_document(payload: bytes, suffix: str) -> str:
    """添付されたシラバスから本文を取り出す。

    **スキャン画像の PDF は扱わない。** 文字が埋め込まれていないものは
    そう言って断る ── OCR に黙って流すと、読み取り誤りがそのまま候補に
    なり、教員には出所が分からない。
    """
    kind = DOCUMENT_SUFFIXES.get(suffix.lower())
    if kind is None:
        raise SyllabusError(f"この形式は読めません（{', '.join(sorted(DOCUMENT_SUFFIXES))}）")
    if kind == "txt":
        return payload.decode("utf-8", "replace").strip()

    from aijudge_ext_document_text import DocumentTextError, text_of

    from aijudge_core import ArtifactKind

    try:
        extracted = text_of(payload, ArtifactKind.PDF if kind == "pdf" else ArtifactKind.DOCX)
    except DocumentTextError as exc:
        raise SyllabusError(f"本文を取り出せませんでした: {exc}") from None
    # **Markdown に均して返す。** そのままだと行が細かく割れていて、
    # 教員が直すにも読みにくい。
    return to_markdown(extracted)


class CourseHint(BaseModel):
    """シラバスから読めたコースの素性。**そのまま登録しない。**"""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=256)
    term: str = Field(default="", max_length=64)
    # 概要・到達目標（Markdown）。コースの `description` に入れる候補。
    description: str = Field(default="", max_length=4000)


class KcHint(BaseModel):
    """KC の候補 1 件。"""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    # 授業計画の何回目から出したか。教員が候補を吟味するときの手がかり。
    source: str = Field(default="", max_length=200)


class SyllabusProposal(BaseModel):
    """モデルに返させる構造化出力（設計原則 P4）。"""

    model_config = ConfigDict(extra="ignore")

    course: CourseHint = CourseHint()
    knowledge_components: tuple[KcHint, ...] = ()


class CourseBasics(BaseModel):
    """シラバスから読んだコースの基本情報。**そのまま保存しない。**"""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=256)
    term: str = Field(default="", max_length=64)
    # 概要・到達目標・授業計画を Markdown に整えたもの。
    markdown: str = Field(default="", max_length=12000)


BASICS_PROMPT = PromptTemplate(
    name="syllabus_to_basics_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは大学のシラバスを読み、そのまま読める Markdown に整える助手です。"
        "**中身を書き換えません。** 要約も補足もせず、見出し・箇条書き・表の体裁だけを"
        "整えます ── 教員が「シラバスにこう書いてあったか」を確かめられなくなるためです。"
        "PDF から抽出した本文は行が細かく割れているので、意味の切れ目で繋ぎ直します。"
    ),
    template=(
        "次のシラバス本文を Markdown に整え、コース名・コード・学期も読み取ってください。\n\n"
        "## シラバス本文\n{text}\n"
    ),
)


PROMPT = PromptTemplate(
    name="syllabus_to_candidates_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    #
    # 2: 既にある知識要素の説明を直した。「重複を作らない」としか書いて
    #    いなかったので、`cs.c_language` が既にある状態で C 言語のシラバスを
    #    渡すと**候補が 0 件**になった（実測 2026-08-31）。モデルが「もう
    #    網羅されている」と読む。既存は**ぶら下げる先**でもあることを言う。
    # 3: 2 の言い方が「分野の根が既にあることは」に限定されていて、**子が
    #    1 件でもあると再び 0 件**になった（実測 2026-08-31）。「一度使うと
    #    使えなくなる」形だった。**既存が何件あっても網羅されたとは考えるな**
    #    と、数に依らない言い方に直した ── 症状ごとに直していると、既存が
    #    増えるたびに同じことが起きる。
    # 4: 問いそのものを変えた。@1〜@3 は「**まだ無いもの**を挙げよ」と訊いて
    #    おり、既存が増えるほど「もう挙げるものが無い」という読みへ圧力が
    #    掛かる（0 件が二度起きた原因はここ）。**このコースが扱う知識要素を
    #    挙げよ**に変え、既にあるものは既存のキーをそのまま書かせる。重複の
    #    禁止は「挙げるな」ではなく「同じ概念に新しいキーを作るな」になり、
    #    モデルが黙る理由が無くなる。教員の側も、このコースが使う範囲
    #    （`Course.knowledge_components`）を決める材料をここで得る（#41）。
    # 5: 骨格を入れた（分野と単位は `subjects/kc/*.yaml` で固定）。
    #    **単位の一覧を渡し、新しい候補は必ずそのどれかの下に置かせる。**
    #    渡さないと、モデルは分野そのものや存在しない単位を作る ── それは
    #    登録できないので、教員が採用しようとして初めて断られる（#157 で
    #    字面について直したのと同じ形の往復が、構造について残っていた）。
    # 6: **既存の知識要素だけを使う**に変えた（2026-09-13 決定）。新しいキーは
    #    提案させず、`existing` の一覧から選ばせる。語彙は骨格（`kc seed`）で
    #    決まり、教員が画面から増やす経路は無くした ── 増やせると同じ概念が
    #    別のキーで二重に登録され、Q-matrix が割れる。
    version="6",
    system=(
        "あなたは大学の理工系コースのシラバスや課題文を読み、"
        "そこで扱う知識要素を**登録済みの一覧から選ぶ**助手です。"
        "**一覧に無い知識要素を作りません。** 本文に書かれていないことも足しません。"
    ),
    template=(
        "## 使える名前空間\n{namespaces}\n\n"
        "## 登録済みの知識要素（この中から選びます）\n"
        "{existing}\n"
        "**キーは上の一覧のものを一字も変えずにそのまま書きます。**"
        "一覧に無いキーを作らないでください ── 登録できず、捨てられます。"
        "近い概念があれば、それを選びます。\n\n"
        "## すること\n"
        "本文を読み、**扱っている知識要素**を一覧から選んで挙げてください。"
        "本文が一覧のどれも扱っていないときだけ、空で返してください。\n\n"
        "## 本文\n{text}\n\n"
        "候補は多くても 20 件までにします。\n"
    ),
)


@dataclass(frozen=True)
class ProposalResult:
    """候補と、それがどう作られたか（再現性のため・P8）。"""

    proposal: SyllabusProposal
    prompt_id: str
    model: str
    # 採用できないので落とした候補と、その理由（#157）。
    # **黙って減らさない。** 20 件出したはずが 14 件しか並んでいないとき、
    # 何が起きたのか画面から分からないのは、間違った候補が並ぶのと同じくらい悪い。
    discarded: tuple[DiscardedCandidate, ...] = ()


class SyllabusReader:
    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 3072,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def read_basics(self, text: str) -> CourseBasics:
        """シラバス本文を Markdown に整え、コースの素性も読む。

        **モデルに整えさせる。** 素の抽出は行が細かく割れていて、見出しも
        表も崩れている。規則で直そうとすると科目ごとの体裁に負ける
        （`to_markdown` はモデルが使えないときの控えである）。
        """
        result = self._gateway.complete_structured(
            BASICS_PROMPT,
            CourseBasics,
            model=self._model,
            # シラバスは公開情報。学習者のデータは含まない（P7）。
            data_class=DataClass.NON_PERSONAL,
            max_tokens=self._max_tokens,
            text=text[:20000],
        )
        return result.value

    def propose(
        self,
        text: str,
        *,
        namespaces: tuple[str, ...],
        existing_keys: tuple[str, ...] = (),
        unit_keys: tuple[str, ...] = (),
    ) -> ProposalResult:
        """貼り付けられたシラバス本文から候補を作る。

        `existing_keys` が**選べる全部**である（2026-09-13 決定: 登録済みの
        知識要素だけを使う）。`unit_keys` は互換のために受けるが使わない。
        """
        result = self._gateway.complete_structured(
            PROMPT,
            SyllabusProposal,
            model=self._model,
            # シラバスは公開情報。学習者のデータは含まない（P7）。
            data_class=DataClass.NON_PERSONAL,
            max_tokens=self._max_tokens,
            namespaces="\n".join(f"- {n}" for n in namespaces) or "（なし）",
            existing="\n".join(f"- {k}" for k in existing_keys) or "（まだありません）",
            text=text[:20000],
        )
        kept, discarded = _screen(result.value, existing_keys=existing_keys)
        return ProposalResult(
            proposal=kept,
            prompt_id=PROMPT.id,
            model=self._model,
            discarded=discarded,
        )


@dataclass(frozen=True)
class DiscardedCandidate:
    """採用できないので落とした候補と、その理由。

    **黙って減らさない。** 20 件出したはずが 14 件しか並んでいないとき、
    何が起きたのか画面から分からないのは、間違った候補が並ぶのと同じくらい悪い。
    """

    key: str
    reason: str


def _screen(
    proposal: SyllabusProposal,
    *,
    existing_keys: tuple[str, ...],
    unit_keys: tuple[str, ...] = (),
) -> tuple[SyllabusProposal, tuple[DiscardedCandidate, ...]]:
    """採用できない候補を落とす。**ここが唯一の関門**（#157）。

    見るのは 1 つ ── **登録済みの知識要素か**。プロンプトは一覧から選べと
    頼んでいるが、頼みは強制ではなく、モデルは言い換えた新しいキーや日本語の
    キーを返す。落とさないと、そのキーは一覧 → 採用まで素通りし、最後の
    登録で初めて弾かれる。**教員は往復し終えてから断られる。**

    以前は形・深さ・置き場所を見て新しいキーを通していたが、語彙を画面から
    増やさない方針（2026-09-13）で、登録済み以外はすべて落とす。`unit_keys`
    は互換のために受けるだけで使わない。
    """
    known = set(existing_keys)
    kept: list[KcHint] = []
    dropped: list[DiscardedCandidate] = []

    for hint in proposal.knowledge_components:
        key = hint.key.strip()
        if key in known:
            kept.append(hint)
            continue
        reason = (
            "キーの形が正しくありません"
            if not is_valid_kc_key(key)
            else "登録済みの知識要素にありません（一覧にあるものだけを使います）"
        )
        dropped.append(DiscardedCandidate(key=hint.key, reason=reason))

    if len(kept) == len(proposal.knowledge_components):
        return proposal, ()
    return proposal.model_copy(update={"knowledge_components": tuple(kept)}), tuple(dropped)


__all__ = [
    "DOCUMENT_SUFFIXES",
    "MAX_SYLLABUS_BYTES",
    "SYLLABUS_EXAMPLE",
    "SYLLABUS_URL_TEMPLATE",
    "CourseBasics",
    "CourseHint",
    "DiscardedCandidate",
    "KcHint",
    "ProposalResult",
    "SyllabusError",
    "SyllabusProposal",
    "SyllabusReader",
    "deep_link",
    "read_document",
    "to_markdown",
]
