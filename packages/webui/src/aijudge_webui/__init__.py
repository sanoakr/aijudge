"""画面の見た目を 1 か所に置く（#184）。

以前は `apps/studentweb` と `apps/reviewconsole` の `base.html` が
`<style>` として同じ CSS を丸ごと 2 部持っていた（221 行が重複）。配色の
定義に至っては 55 行が完全に一致していて、そのコメント自身が「片方だけ
直すと気づきにくい壊れ方をする」と警告していた ── その警告は 1 ファイル内
の話だったが、2 ファイルの間でも同じことが起きるし、防ぐ仕組みは無かった。

**このパッケージはファイルとパスだけを持つ。** 依存は無く、配信の仕組みも
知らない（`packages/telemetry` と同じ形）。`StaticFiles` で mount するのは
プロセスを組み立てる側 ── `apps/*` の仕事である。見た目の資産が「どう
配信されるか」を決め始めると、配信の都合が画面に混ざる。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "ASSETS_DIR",
    "DEFAULT_TIMEZONE",
    "ENV_TIMEZONE",
    "GUIDE_URL",
    "STATIC_MOUNT",
    "TEMPLATES_DIR",
    "asset_url",
    "display_zone",
    "from_local",
    "guide_url",
    "local_filter",
    "to_local",
]

#: CSS の置き場所。`StaticFiles(directory=ASSETS_DIR)` に渡す。
ASSETS_DIR = Path(__file__).parent / "assets"
#: 両アプリが共有するテンプレート断片。Jinja の `ChoiceLoader` に足す。
TEMPLATES_DIR = Path(__file__).parent / "templates"
#: 配信するパス。**両アプリで同じ**にする ── 違えると共有の断片
#: （`_theme.html`）がどちらか片方でしか解決できない。
STATIC_MOUNT = "/static"

#: 利用ガイドの公開先（`mkdocs.yml` の `site_url`）。
#:
#: **ここに 1 つだけ持つ。** 両アプリのヘッダと 4 つの入口から指すので、
#: 書き写すと出す場所を増やした日にどれかが古いままになる。公開先が変わる
#: ときに直すのもここ 1 か所でよい。
#:
#: 機関固有の値ではない（この製品自身の文書の置き場）ので、`deploy.sh` の
#: ホスト名と違って設定には出さない ── 読む人にとっては製品の一部である。
GUIDE_URL = "https://sanoakr.github.io/aijudge/"


def guide_url(page: str = "") -> str:
    """利用ガイドの URL。`page` はガイドの中の頁（`mkdocs.yml` の `nav`）。

    **読者に合わせた頁へ送る。** 学習者アプリから索引に落とすと、学生は
    TA 向け・教員向けと並んだ一覧から自分の頁を選ぶことになる ── 入口の
    案内としては 1 段遠い。教員コンソールは TA と教員の両方が使うので、
    そちらは索引でよい。

    `page` に知らない名前を渡しても止めない ── 壊れるのは行き先であって
    画面ではなく、ここで例外を投げると案内を出そうとして画面が落ちる。
    """
    return f"{GUIDE_URL}{page}/" if page else GUIDE_URL


def asset_url(name: str, *, version: str = "", prefix: str = "") -> str:
    """資産への URL を作る。

    `version` を付けるのは**キャッシュを外すため**。付けないと、配置し直した
    のにブラウザが前の CSS を使い続け、症状は「配置に失敗した」ように見える。
    渡すのは `app_version` でよい（版が上がったときだけ URL が変わる）。

    `prefix` は逆プロキシ下の接頭辞（教員コンソールの
    `AIJUDGE_CONSOLE_ROOT_PREFIX`）。**ここを忘れると 404 になる** ── 同じ
    忘れ方を一度出荷している（#165 の区画リンク）。
    """
    url = f"{prefix}{STATIC_MOUNT}/{name}"
    return f"{url}?v={version}" if version else url


# ── 表示のタイムゾーン ─────────────────────────────────────
#
# 日時は **UTC で保存し、表示だけ機関の時刻に直す**（`UtcDateTime`）。
# 以前は保存した UTC を `strftime` でそのまま出していたので、提出日時も
# 締切も 9 時間ずれて見えた（提出 13:26 JST が「04:26」）。締切の入力欄も
# 同じ ── 教員が JST のつもりで打った時刻を UTC として保存していた。
#
# 機関の時刻は環境変数で決める。**コードに機関を焼き込まない**が、既定は
# 運用している場所（日本）に合わせる ── 未設定で UTC に落ちると、いちばん
# 多い配備で全員の時刻がずれる。
ENV_TIMEZONE = "AIJUDGE_TIMEZONE"
DEFAULT_TIMEZONE = "Asia/Tokyo"


def display_zone() -> tzinfo:
    """表示に使うタイムゾーン。名前が不正なら既定に落とし、黙って UTC にはしない。"""
    name = os.environ.get(ENV_TIMEZONE, "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def to_local(value: datetime | None) -> datetime | None:
    """保存された日時（aware・UTC）を表示のタイムゾーンへ。naive は UTC とみなす。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(display_zone())


def from_local(value: datetime) -> datetime:
    """入力欄の日時（naive・表示のタイムゾーン）を保存用の UTC に。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=display_zone())
    return value.astimezone(UTC)


def local_filter(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Jinja のフィルタ `local`。`{{ x | local('%m-%d %H:%M') }}`。None は空。"""
    converted = to_local(value)
    return "" if converted is None else converted.strftime(fmt)
