"""画面の文言に Markdown の記法が混ざっていないことを機械で保証する（#191）。

`**すべてのセッションが切れます**` が、パスワードを変えるかどうかを決める
画面にアスタリスクごと出ていた ── いちばん落ち着いて読ませたい一文である。

**なぜ再発するか。** このリポジトリはコメントの強調に `**…**` を使う。
Python の docstring、Jinja のコメント、CSS のコメント、どこでもそうで、
それは作法として正しい。だが**テンプレートの本文も散文**であり、書いている
手には区別が無い。気づける仕組みが無かったので、2 か所で同じ滑り方をした
（1 件は #190 のレビューで捕まえた）。

**コメントは対象外。** `test_ui_copy_has_no_issue_numbers.py` と同じ線引きで、
見るのは「ブラウザに文字として出るもの」だけである。コメントの `**…**` は
むしろ残すべきもので、ここで数えない。

テンプレートで強調するなら `<strong>` を書く。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_DIRS = (
    REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "templates",
    REPO_ROOT / "apps" / "studentweb" / "src" / "aijudge_studentweb" / "templates",
    # 両アプリが読む共有の断片（#184）。ここも画面に出る。
    REPO_ROOT / "packages" / "webui" / "src" / "aijudge_webui" / "templates",
)

# `**強調**`。**改行を挟まないものだけ見る** ── コメントを落としたあとに
# 残る `*` は掛け算や CSS の `*` セレクタでもありうるので、対になっていて
# 中身が 1 行に収まっているものに限る。
MARKDOWN_STRONG = re.compile(r"\*\*[^*\n]{1,80}\*\*")


def _visible_text(template: str) -> str:
    """テンプレートから、ブラウザに文字として出ない部分を落とす。

    落とすものは `test_ui_copy_has_no_issue_numbers.py` と同じ ── 線引きを
    2 つ持つと、片方だけ直したときにもう片方が取り残される。
    """
    text = re.sub(r"\{#.*?#\}", "", template, flags=re.S)  # Jinja のコメント
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    return re.sub(r"<script.*?</script>", "", text, flags=re.S)


def test_templates_do_not_show_markdown_emphasis() -> None:
    leaks: list[str] = []
    checked = 0
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.glob("*.html")):
            checked += 1
            for match in MARKDOWN_STRONG.finditer(_visible_text(path.read_text(encoding="utf-8"))):
                leaks.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert checked > 20, "検査対象が少なすぎる（走査に失敗している）"
    assert not leaks, (
        "テンプレートの本文に Markdown の強調が混ざっている。"
        "ブラウザにはアスタリスクがそのまま出る。<strong> を書くこと:\n  " + "\n  ".join(leaks)
    )


def test_emphasis_in_comments_is_not_counted() -> None:
    """コメントの `**…**` は作法。**ここで数えたら規約と喧嘩する。**

    この線引きが逆向きに壊れる（コメントまで弾く）と、判断の記録を
    書けなくなる ── それはこのリポジトリがいちばん大事にしているもので、
    テストが規約を壊すことになる。
    """
    template = (
        "{# **これはコメントの強調で、残すべきもの** #}\n"
        "<p>ふつうの文</p>\n"
        "<script>// **これも出ない**</script>\n"
    )
    assert not MARKDOWN_STRONG.findall(_visible_text(template))
    # 本文に入れたら捕まること（テストが常に通るだけの飾りになっていない）。
    assert MARKDOWN_STRONG.findall(_visible_text("<p>**出てしまう**</p>"))
