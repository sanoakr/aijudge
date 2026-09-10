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

from pathlib import Path

__all__ = ["ASSETS_DIR", "STATIC_MOUNT", "TEMPLATES_DIR", "asset_url"]

#: CSS の置き場所。`StaticFiles(directory=ASSETS_DIR)` に渡す。
ASSETS_DIR = Path(__file__).parent / "assets"
#: 両アプリが共有するテンプレート断片。Jinja の `ChoiceLoader` に足す。
TEMPLATES_DIR = Path(__file__).parent / "templates"
#: 配信するパス。**両アプリで同じ**にする ── 違えると共有の断片
#: （`_theme.html`）がどちらか片方でしか解決できない。
STATIC_MOUNT = "/static"


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
