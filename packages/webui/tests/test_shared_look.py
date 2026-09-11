"""共有の見た目が「ファイルとパスだけ」であることを固定する（#184）。

このパッケージは配り方を知らない。`StaticFiles` で mount するのは合成ルート
（`apps/*`）の仕事で、ここが配信の仕組みを持ち始めると、配信の都合が画面に
混ざる（`packages/telemetry` と同じ形）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import aijudge_webui as webui

MANIFEST = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_assets_are_shipped_with_the_package() -> None:
    """CSS が**パッケージの中**にあること。

    リポジトリ直下に置くと、wheel に入らないので配置した先で 404 になる
    （開発中は動くので気づかない）。
    """
    assert webui.ASSETS_DIR.is_dir()
    assert {p.name for p in webui.ASSETS_DIR.iterdir()} == {
        "base.css",
        "learner.css",
        "console.css",
    }
    assert webui.TEMPLATES_DIR.is_dir()
    assert {p.name for p in webui.TEMPLATES_DIR.iterdir()} == {
        "_demo_banner.html",
        "_rail.html",
        "_theme_boot.html",
        "_theme_switch.html",
    }
    for path in webui.ASSETS_DIR.iterdir():
        assert path.stat().st_size > 0, f"{path.name} が空"


def test_the_url_carries_a_version_so_the_browser_lets_go_of_the_old_sheet() -> None:
    """版を付けないと、配置し直してもブラウザが前の CSS を使い続ける。

    症状は「配置に失敗した」ように見えるので、原因に辿り着くのが遅い。
    """
    assert webui.asset_url("base.css", version="1.2.3") == "/static/base.css?v=1.2.3"
    # 版が無いときは付けない（開発中に URL が毎回変わると読みにくい）。
    assert webui.asset_url("base.css") == "/static/base.css"


def test_the_url_goes_through_the_reverse_proxy_prefix() -> None:
    """接頭辞つき配置（`AIJUDGE_CONSOLE_ROOT_PREFIX`）で 404 にしない。

    同じ忘れ方を一度出荷している ── `/console` の下で区画へのリンクが
    404 になっていた（#165）。
    """
    assert webui.asset_url("base.css", prefix="/console") == "/console/static/base.css"
    assert (
        webui.asset_url("base.css", version="1.2.3", prefix="/console")
        == "/console/static/base.css?v=1.2.3"
    )


def test_the_shared_look_declares_no_dependencies() -> None:
    """依存を持たないこと。

    ここに `fastapi` が入るのは、見た目の資産が「どう配信されるか」を
    決め始めた兆候である。そのとき、画面を差し替えるのに配信の層を
    触ることになる。
    """
    with MANIFEST.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    assert dependencies == [], f"webui gained unexpected dependencies: {dependencies}"


def test_both_apps_serve_it_from_the_same_path() -> None:
    """mount 先は両アプリで同じ。

    違えると共有のテンプレート断片（`_theme_switch.html`）が
    どちらか片方でしか解決できない ── 断片は URL を `static_url()` に
    任せているので、名前が同じであることが前提になっている。
    """
    assert webui.STATIC_MOUNT == "/static"


def test_a_draft_statement_wraps_but_code_does_not() -> None:
    """**問題文は折り返す。コードは折り返さない**（#266）。

    承認待ちの課題は `<pre class="statement">` で、生成されたままの文面を
    出す（整形すると、モデルが何を書いたのかが分からなくなる）。既定の
    `white-space: pre` のままだと、AI が書いた長い 1 行がそのまま伸びる ──
    390px の画面で 2092px はみ出していた。

    コードは事情が違う。折り返すと意味が変わるので、横スクロールに逃がす。
    **両方を 1 つの試験で見る** ── 片方だけ直して、もう片方を巻き添えに
    するのがこの手の修正の失敗の形である。
    """
    css = (webui.ASSETS_DIR / "base.css").read_text(encoding="utf-8")
    assert "pre.statement{white-space:pre-wrap" in css, "問題文が折り返さない"
    assert "pre.code{margin:0;overflow-x:auto" in css, "コードが横スクロールでなくなった"
