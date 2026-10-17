"""画像が最後まであるかを、受付の時点で確かめる（#633）。

## なぜ受付で見るか

途中で切れた画像は、受付を通っても採点で初めて読めないと分かる。2026-10-16 の
network ex5/paiza5 では、認定証の PNG 2 枚が 393,216 バイトで切れており（`IEND`
が無い）、書き起こしは主系・従系とも `Failed to load image` で失敗して人へ回った。
学生はそのとき画面を離れているので、出し直す機会がその場では無かった。受付で
断れば、その場で保存し直して出し直せる。

## 何を見て、何を見ないか

**デコードはしない。終わりの印だけを見る。** 画像処理の依存（Pillow）を持たない
のは ADR 0021 の判断で、ここもそれに従う。形式は拡張子ではなく先頭の印で決める
（`.png` という名前の JPEG は実際にある）。

- PNG: チャンクを順にたどり、長さが尽きずに `IEND` まで届けば完全。`IEND` の後ろに
  余分なバイトがあっても通す
- JPEG: `FFD8` で始まり、終わりの印 `FFD9` がどこかにあれば通す。**末尾を見ない**
  ── スマートフォンの「動く写真」は JPEG の後ろに動画をつなげて保存するので、末尾が
  `FFD9` とは限らない。サムネイルの `FFD9` で切れた JPEG を見逃すことはあるが、
  誤って断るより見逃すほうを選ぶ（断られた学生は、出す手段を失う）
- それ以外（GIF・WebP・HEIC など）は確かめずに通す
"""

from __future__ import annotations

import struct

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"

# PNG のチャンク: 長さ（4）・種類（4）・データ（長さ分）・CRC（4）。
_CHUNK_HEADER = 8
_CHUNK_CRC = 4

INCOMPLETE_IMAGE = (
    "画像ファイルが壊れているか、途中で切れています。保存し直してから提出してください"
)


def _png_is_complete(payload: bytes) -> bool:
    offset = len(PNG_SIGNATURE)
    while offset + _CHUNK_HEADER <= len(payload):
        (length,) = struct.unpack(">I", payload[offset : offset + 4])
        kind = payload[offset + 4 : offset + 8]
        end = offset + _CHUNK_HEADER + length + _CHUNK_CRC
        if end > len(payload):
            return False
        if kind == b"IEND":
            return True
        offset = end
    return False


def image_is_complete(payload: bytes) -> bool:
    """最後まであるか。**判定できない形式は真**（断らない）。"""
    if payload.startswith(PNG_SIGNATURE):
        return _png_is_complete(payload)
    if payload.startswith(JPEG_START):
        return JPEG_END in payload[len(JPEG_START) :]
    return True
