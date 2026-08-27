"""aiJudge eval runner — 採点精度の測定コマンド。

**採点を実行しない。** 記録済みの観測レコードを読み、指標を計算して
合格基準と突き合わせるだけ（ADR 0007）。採点運用はこのパッケージに依存せず、
削除しても採点は動く。それを `.importlinter` の契約で保証している
（core・grading・llm_gateway・sandbox を import しない）。
"""

from __future__ import annotations

from .observations import (
    ENV_GOLDEN_DIR,
    ObservationSetError,
    golden_root,
    iter_observations,
    load_observations,
)
from .runner import EvalReport, measure

__all__ = [
    "ENV_GOLDEN_DIR",
    "EvalReport",
    "ObservationSetError",
    "golden_root",
    "iter_observations",
    "load_observations",
    "measure",
]
