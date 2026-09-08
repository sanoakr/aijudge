"""学期の語彙（#167）。

`Course.term` は自由文字列だったので表記がゆれた。ゆれは表示の問題では
**ない** ── コースの同一性は (テナント, コード, 学期) で、ID はそこから
導かれる（`aijudge_admin.operations.ensure_course`）。`2025-後期` と
`2025後期` は同じ授業のつもりでも別のコースになり、受講登録も提出も
別々に積まれる。

**規則の置き場所をここ 1 つにする。** 画面（コース作成フォーム）と CLI
（`aijudge-admin course create --term`）が別々に組み立てると、片方だけが
古い形を作る日が来る ── そして作られた ID はもう変えられない。

正準形式は `YYYY-<区分>`（例 `2026-前期`）。**既存データと同じ形である**
（`2025-後期` / `2026-前期` / `2026-後期` しか無かった）ので、移行は要らない。
これは幸運ではなく制約で、形を変えると既存コースの ID を作り直すことに
なる（採点結果が課題版を指しており、課題版の ID はコースから導かれる）。

`YYYY` は**年度**である。2026-1Q は 2026 年度の第 1 クォーターで、暦年
ではない。保存値には「年度」の語を入れない ── 入れると既存データと別形式に
なり、ID を変えられないので新旧 2 つの表記が並ぶ期間ができる。
"""

from __future__ import annotations

import re
from datetime import date

# 学期の区分。**この並びが選択肢の順でもあり、並べ替えの序数でもある。**
# 2 つに分けると、片方だけを直したときに画面の並びと一覧の並びが食い違う。
#
# `通年`・`集中` を最後に置くのは、どちらも特定の四半期に属さないため
# （教員の指定・2026-09-08）。
DIVISIONS: tuple[str, ...] = ("前期", "後期", "1Q", "2Q", "3Q", "4Q", "通年", "集中")

# 選択肢に出す年度の数（今年度から）。
YEARS_OFFERED = 3

# 年度が切り替わる月。日本の大学の年度は 4 月に始まる。
_FISCAL_START_MONTH = 4

_TERM_RE = re.compile(r"^(\d{4})-(.+)$")


def academic_year(today: date | None = None) -> int:
    """いまの年度。1〜3 月は前の年の年度である。"""
    at = today or date.today()
    return at.year if at.month >= _FISCAL_START_MONTH else at.year - 1


def offered_years(today: date | None = None, *, count: int = YEARS_OFFERED) -> tuple[int, ...]:
    """選ばせる年度。**今年度から先だけ。**

    過去の年度を選択肢に出さないのは、コースを遡って作る操作が無いため
    （過去のコースは既にあり、学期は作成後に変えられない）。
    """
    start = academic_year(today)
    return tuple(start + offset for offset in range(count))


def format_term(year: int, division: str) -> str:
    """正準形式を組み立てる。知らない区分なら ValueError。"""
    if division not in DIVISIONS:
        raise ValueError(f"知らない学期の区分です: {division!r}（{'・'.join(DIVISIONS)}）")
    if not 1000 <= year <= 9999:
        raise ValueError(f"年度は 4 桁で書きます: {year!r}")
    return f"{year}-{division}"


def parse_term(term: str) -> tuple[int, str]:
    """正準形式を年度と区分に割る。形が不正なら ValueError。"""
    matched = _TERM_RE.match(term)
    if matched is None:
        raise ValueError(f"学期は `年度-区分` の形です: {term!r}")
    division = matched.group(2)
    if division not in DIVISIONS:
        raise ValueError(f"知らない学期の区分です: {division!r}（{'・'.join(DIVISIONS)}）")
    return int(matched.group(1)), division


def is_valid_term(term: str) -> bool:
    """正準形式として通るか。**例外ではなく真偽で答える**（`is_valid_kc_key` と同じ）。"""
    try:
        parse_term(term)
    except ValueError:
        return False
    return True


def term_sort_key(term: str) -> tuple[int, int, int, str]:
    """一覧の並び順。**文字列順ではなく (年度, 区分の順) で並べる。**

    以前は SQL の `ORDER BY term` に任せていた。`前期`／`後期` はたまたま
    文字コード順が時系列と一致するが、`1Q`〜`4Q` が混ざると一致しない
    （`1Q` < `前期` になる）。

    **正準形式でない値は最後に、文字列順で置く。** 自由入力だった時代の値が
    残っていても落ちないため ── そして「並びがおかしい」ことが見えるのは、
    黙って前期の隣に混ぜるより良い。
    """
    try:
        year, division = parse_term(term)
    except ValueError:
        return (1, 0, 0, term)
    return (0, year, DIVISIONS.index(division), "")


__all__ = [
    "DIVISIONS",
    "YEARS_OFFERED",
    "academic_year",
    "format_term",
    "is_valid_term",
    "offered_years",
    "parse_term",
    "term_sort_key",
]
