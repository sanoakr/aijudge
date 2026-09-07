"""画面の文言に issue 番号が混ざっていないことを機械で保証する（#139）。

`（#127）` のような開発者向けの参照が画面に出ていた。実装の経緯はコミット・
PR・**コード内コメント**に残せば十分で、利用者が読む文面に混ぜるものではない
── 学生や教員にとって番号は意味を持たず、「何かの手続き番号か」と読ませる。

#139 は文言を直したがテストを残さなかったため、新しい画面を足したときに
同じ形で再発した（#144 の作業中に実際に再発した）。だからここで固定する。

**コメントは対象外。** Jinja のコメント（`{# … #}`）と Python の docstring・
コメントには番号を残す ── むしろ残すべきで、それが #139 の結論だった。
見るのは「ブラウザに出る文字列」だけである。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_DIRS = (
    REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "templates",
    REPO_ROOT / "apps" / "studentweb" / "src" / "aijudge_studentweb" / "templates",
)

# `#127` のような参照。`#141e2b`（CSS の色）は数字が 5 桁以上・英字混じりで外れ、
# `&#10;`（実体参照）は直前の `&` で外す。
ISSUE_REFERENCE = re.compile(r"(?<!&)#\d{1,4}(?!\w)")
# 日本語を含む文字列だけを画面の文言とみなす（識別子・URL・SQL を拾わないため）。
JAPANESE = re.compile(r"[ぁ-んァ-ヶ一-龥]")

# 画面・API に出る文面を作る呼び出し。
USER_FACING_CALLS = frozenset(
    {
        "HTTPException",
        "AdminError",
        "AuthenticationFailed",
        "PermissionDenied",
        "SubmissionRejected",
        "TemplateResponse",
    }
)


def _visible_text(template: str) -> str:
    """テンプレートから、ブラウザに文字として出ない部分を落とす。"""
    text = re.sub(r"\{#.*?#\}", "", template, flags=re.S)  # Jinja のコメント
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)  # CSS（色が #rrggbb）
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S)
    return re.sub(r"&#\d+;", "", text)  # HTML の実体参照（`&#10;`）


def test_templates_do_not_show_issue_numbers() -> None:
    leaks: list[str] = []
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.glob("*.html")):
            for match in ISSUE_REFERENCE.finditer(_visible_text(path.read_text(encoding="utf-8"))):
                leaks.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert not leaks, "画面の文言に issue 番号が出ている: " + ", ".join(leaks)


def test_user_facing_messages_do_not_show_issue_numbers() -> None:
    """例外の `detail` や保存の合図など、Python 側が作る文面も見る。

    docstring とコメントは対象外（`ast` は docstring を式として残すが、
    ここでは「画面に出る呼び出しの引数」と「辞書の値」だけを見る）。
    """
    leaks: list[str] = []
    for tree_name in ("apps", "packages"):
        for path in sorted((REPO_ROOT / tree_name).rglob("*.py")):
            if "/tests/" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - 構文が壊れていれば他が落ちる
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name not in USER_FACING_CALLS:
                        continue
                    texts = [
                        sub.value
                        for sub in ast.walk(node)
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    ]
                elif isinstance(node, ast.Dict):
                    # `SAVED_MESSAGES` のような「画面に出す文面の表」。
                    texts = [
                        value.value
                        for value in node.values
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    ]
                else:
                    continue
                for text in texts:
                    if JAPANESE.search(text) and ISSUE_REFERENCE.search(text):
                        leaks.append(f"{path.relative_to(REPO_ROOT)}: {text[:60]}")
    assert not leaks, "画面に出る文面に issue 番号が出ている: " + ", ".join(leaks)
