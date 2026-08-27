"""aiJudge review console — 採点結果を教員が確認して確定させる。

**採点はここでは走らない。** 採点はワーカー（`aijudge-worker`）が提出時に
走らせ、コンソールは届いた結果を読むだけ。レビューを採点の前提条件に
しないのは、測定用データの入力が採点を止めてはならないため（ADR 0007）。

blind 採点（AI を見る前に教員が段階を付ける）は**抽出された提出のみ**に
求める。順序を逆にすると教員の採点が AI に引きずられ、一致度の測定に
使えなくなるが、全件に課すとレビューのたびに 2 段階の入力を強制する。
"""

from __future__ import annotations

from .app import SESSION_COOKIE, Console, create_app
from .sampling import is_blind_sample

__all__ = ["SESSION_COOKIE", "Console", "create_app", "is_blind_sample"]
