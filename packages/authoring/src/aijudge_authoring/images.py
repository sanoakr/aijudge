"""課題文に貼る画像の置き場所と鍵。

**描画はすでに通る**（`statement.py` の Markdown が `![](URL)` を `<img>` に
する）。足りないのは置き場所で、外部 URL を書かせると 2 つ壊れる ── 課題文を
開くたびに学習者のブラウザが学外へ取りに行き（P7）、外部の都合で課題文の
一部だけが見えない日ができる（数式を CDN ではなくサーバ側 MathML にしたのと
同じ理由・`statement.py` 冒頭）。

**提出物と同じストアに置く**（`ArtifactStore`）。保存先を増やさない。

**鍵にコースを含める。** 画像の URL からコースが分かるので、誰に見せてよいか
を DB を引かずに決められる ── 新しい表を作らずに済む、という以上に、
「この画像は何のものか」を鍵そのものが持っているほうが失われにくい。

URL は**両方のアプリで同じ経路**にする（`/images/...`）。課題文は学習者にも
教員にも出るので、絶対 URL を埋め込むとどちらかのホスト名が課題文に焼き付く。
相対パスなら、開いている側のアプリが自分で返す。
"""

from __future__ import annotations

import hashlib
import re

# 受け付ける形式。**課題文に貼るものだけ。** 提出物の形式（`uploads.py`）とは
# 別に持つ ── あちらは学習者が出すもので、PDF のように「貼れないが提出はできる」
# ものがある。
SUFFIX_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

# 1 枚あたりの上限。課題文の挿絵で 4MB を超えるものは、たいてい画面
# いっぱいのスクリーンショットを縮めずに貼っている。
MAX_BYTES = 4 * 1024 * 1024

_PREFIX = "statement-images"
_NAME = re.compile(r"^[0-9a-f]{32}$")


class ImageError(Exception):
    """貼れない画像。理由を添えて上位に返す。"""


def suffix_of(filename: str) -> str:
    """ファイル名から拡張子を取る。**知らない形式は受け付けない。**"""
    lowered = filename.lower()
    for suffix in SUFFIX_TYPES:
        if lowered.endswith(suffix):
            return suffix
    raise ImageError(
        f"{filename} は課題文に貼れません。使えるのは " + "・".join(sorted(SUFFIX_TYPES)) + " です"
    )


def storage_key(course_id: str, name: str) -> str:
    """ストア上の鍵。**URL から機械的に導ける形にする。**

    導けないと、URL と鍵の対応を別に持つことになり、その表が失われた時点で
    課題文の画像が全部行方不明になる。
    """
    suffix = suffix_of(name)
    stem = name[: -len(suffix)]
    if not _NAME.match(stem):
        # 経路を含む名前を鍵にしない（`../` でストアの外へ出る）。
        raise ImageError(f"画像の名前が不正です: {name!r}")
    return f"{_PREFIX}/{course_id}/{name}"


def content_type(name: str) -> str:
    return SUFFIX_TYPES[suffix_of(name)]


def new_name(payload: bytes, filename: str) -> str:
    """保存する名前。**中身から導く。**

    同じ画像を 2 回貼っても 1 枚で済み、教員が同じファイルを貼り直しても
    課題文の URL が変わらない（版が上がらない）。
    """
    if not payload:
        raise ImageError("中身が空です")
    if len(payload) > MAX_BYTES:
        mb = MAX_BYTES // (1024 * 1024)
        raise ImageError(f"{mb}MB を超える画像は貼れません（{len(payload) // 1024}KB あります）")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"{digest}{suffix_of(filename)}"


def markdown_for(course_id: str, name: str, alt: str = "") -> str:
    """課題文に貼り付ける 1 行。**教員に URL を手で書かせない。**

    打ち間違いは課題文の欠損として出る（画像が出ないだけで、なぜ出ないかは
    画面から分からない）。
    """
    return f"![{alt}](/images/{course_id}/{name})"


__all__ = [
    "MAX_BYTES",
    "SUFFIX_TYPES",
    "ImageError",
    "content_type",
    "markdown_for",
    "new_name",
    "storage_key",
    "suffix_of",
]
