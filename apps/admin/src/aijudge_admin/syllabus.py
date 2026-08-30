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

PDF の抽出は採点側と同じもの（`aijudge_norm_document_text.text_of`）を使う。
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

    from aijudge_norm_document_text import DocumentTextError, text_of

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
    version="3",
    system=(
        "あなたは大学の理工系コースのシラバスを読み、"
        "そのコースの素性と、そこで扱う知識要素の候補を取り出す助手です。"
        "**シラバスに書かれていないことを足しません。**"
        "知識要素は「何ができれば身に付いたと言えるか」の粒度にします ── "
        "章や回の題名をそのまま写すのではありません。"
    ),
    template=(
        "## 使える名前空間\n{namespaces}\n\n"
        "## 既にある知識要素\n"
        "{existing}\n"
        "**同じものを作らないでください。それだけが、この一覧の意味です。**\n"
        "**既にある数がいくつであっても、網羅されたとは考えないでください。**"
        "1 件も無いときと 20 件あるときとで、することは変わりません ── "
        "シラバスを読み、そこで扱われていて**この一覧に無いもの**を挙げてください。\n"
        "既にあるものの下に子を足すのが普通の形です。"
        "該当するものが本当に 1 つも無いときだけ、空で返してください。\n\n"
        "## シラバス本文\n{text}\n\n"
        "知識要素のキーは `名前空間.親.子` の形にし、英小文字・数字・下線だけを使います。\n"
        "**親が既にあるものを優先し**、無い場合は親から順に挙げてください。\n"
        "候補は多くても 20 件までにします。\n"
    ),
)


@dataclass(frozen=True)
class ProposalResult:
    """候補と、それがどう作られたか（再現性のため・P8）。"""

    proposal: SyllabusProposal
    prompt_id: str
    model: str


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
    ) -> ProposalResult:
        """貼り付けられたシラバス本文から候補を作る。"""
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
        return ProposalResult(proposal=result.value, prompt_id=PROMPT.id, model=self._model)


__all__ = [
    "DOCUMENT_SUFFIXES",
    "MAX_SYLLABUS_BYTES",
    "SYLLABUS_EXAMPLE",
    "SYLLABUS_URL_TEMPLATE",
    "CourseBasics",
    "CourseHint",
    "KcHint",
    "ProposalResult",
    "SyllabusError",
    "SyllabusProposal",
    "SyllabusReader",
    "deep_link",
    "read_document",
    "to_markdown",
]
