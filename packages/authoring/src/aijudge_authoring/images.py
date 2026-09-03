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

# 貼ったときの既定の表示幅（px）。**課題文の本文と同じ幅に収める。**
# 縮めずに貼ると、写真 1 枚で画面が埋まり、課題文の続きが画面外へ出る
# （4000px の写真をそのまま貼ると実際にそうなる）。
#
# **高さは指定しない。** 幅だけを言えば縦横比は保たれる ── 両方を書くと、
# 教員が幅だけを直したときに絵が歪む。描画側の CSS も `height:auto`。
DISPLAY_WIDTH = 480

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


def intrinsic_width(payload: bytes) -> int | None:
    """画像そのものの幅（px）。分からなければ `None`。

    **符号の先頭だけを読む。** 表示幅を決めるのに要るのは幅だけで、そのために
    画像を展開する依存を増やしたくない（描画は課題文の表示側の仕事であって、
    貼り付けの仕事ではない）。読めない形式は `None` を返し、幅を書かない
    ── 分からない数を書くより、書かないほうがよい。
    """
    if payload[:8] == b"\x89PNG\r\n\x1a\n" and payload[12:16] == b"IHDR":
        return int.from_bytes(payload[16:20], "big")
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(payload[6:8], "little")
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return _webp_width(payload)
    if payload[:2] == b"\xff\xd8":
        return _jpeg_width(payload)
    # SVG は画素を持たない（表示側で伸びる）。幅は書かない。
    return None


def _jpeg_width(payload: bytes) -> int | None:
    """JPEG の SOF から幅を取る。**節を順に飛ばす。**"""
    at = 2
    size = len(payload)
    while at + 9 < size:
        if payload[at] != 0xFF:
            return None
        marker = payload[at + 1]
        # スタンドアロンの印（長さを持たない）は読み飛ばす。
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:
            at += 2
            continue
        length = int.from_bytes(payload[at + 2 : at + 4], "big")
        # SOF0〜SOF15（DHT・JPG・DAC を除く）が寸法を持つ節。
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(payload[at + 7 : at + 9], "big")
        if length < 2:
            return None
        at += 2 + length
    return None


def _webp_width(payload: bytes) -> int | None:
    """WebP の 3 つの型（可逆・非可逆・拡張）から幅を取る。"""
    kind = payload[12:16]
    if kind == b"VP8 " and len(payload) >= 30:
        return int.from_bytes(payload[26:28], "little") & 0x3FFF
    if kind == b"VP8L" and len(payload) >= 25:
        bits = int.from_bytes(payload[21:25], "little")
        return (bits & 0x3FFF) + 1
    if kind == b"VP8X" and len(payload) >= 27:
        return int.from_bytes(payload[24:27], "little") + 1
    return None


def display_width(payload: bytes) -> int | None:
    """貼るときに書く表示幅（px）。**縮めるときだけ書く。**

    元より大きく引き伸ばさない ── 小さな図を無理に広げると粗くなるだけで、
    貼った教員は「なぜぼやけるのか」を画面から知りようがない。
    """
    width = intrinsic_width(payload)
    if width is None or width <= DISPLAY_WIDTH:
        return None
    return DISPLAY_WIDTH


def markdown_for(course_id: str, name: str, alt: str = "", width: int | None = None) -> str:
    """課題文に貼り付ける 1 行。**教員に URL を手で書かせない。**

    打ち間違いは課題文の欠損として出る（画像が出ないだけで、なぜ出ないかは
    画面から分からない）。

    表示幅は `{width=...}` で書く（`statement.py` が `width` だけを通す）。
    **教員があとから数字を書き換えられる形にしておく** ── 既定の幅が合わない
    課題は必ずあり、そのために画像を貼り直させたくない。
    """
    line = f"![{alt}](/images/{course_id}/{name})"
    return f"{line}{{width={width}}}" if width else line


__all__ = [
    "DISPLAY_WIDTH",
    "MAX_BYTES",
    "SUFFIX_TYPES",
    "ImageError",
    "content_type",
    "display_width",
    "intrinsic_width",
    "markdown_for",
    "new_name",
    "storage_key",
    "suffix_of",
]
