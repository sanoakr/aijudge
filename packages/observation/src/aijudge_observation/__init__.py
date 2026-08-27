"""観測レコード — 採点とレビューが測定のために残す記録。

**記録と計算は別物である**（ADR 0007）。

- **記録（このパッケージ）は Phase 0** — 後から再構成できない。AI の判定を
  見た教員の採点は、遡って blind にできない。
- **計算（`aijudge_analytics`）は Phase 1** — 記録が揃っていれば、いつでも
  後から計算できる。

だから記録の形式は Phase 0 の側に置く。ここを測定パッケージの中に置くと、
測定を削除したときに採点側の import が壊れる。**それは実際に壊れた**
（2026-08-28、`packages/analytics` を消して採点を走らせる実験）。
「測定は必須機能でない」は主張ではなく、確かめる対象である。

このパッケージは採点も測定も知らない。依存は pydantic だけ。
"""

from __future__ import annotations

from .record import Observation

__all__ = ["Observation"]
