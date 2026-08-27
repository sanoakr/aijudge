"""blind 採点の抽出。

**全件 blind にしない。** レビュー 1 件ごとに 2 段階の入力を強制することに
なり、測定コストを日常業務に転嫁する（ADR 0007）。

**教員に選ばせない。** 難しい提出だけ blind にするといった選択バイアスが
入ると、一致度がその分だけ意味を失う。

したがって提出 ID のハッシュで決める。決定的（同じ提出は毎回同じ判定）で、
比率は科目プロファイルが宣言する。
"""

from __future__ import annotations

import hashlib

_SAMPLE_BITS = 64


def is_blind_sample(submission_id: str, rate: float) -> bool:
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(submission_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / (1 << _SAMPLE_BITS)
    return bucket < rate
