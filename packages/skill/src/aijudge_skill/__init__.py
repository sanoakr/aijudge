"""S7: 知識要素・Q-matrix・習熟度推定。

設計方針 §2.2 の S7。**採点（S5）を知らず、イベントだけで繋がる**（P6）。
このパッケージを削除しても採点は完了する（P2）。
"""

from .bkt import (
    DEFAULT_CORRECT_THRESHOLD,
    BktParameters,
    is_correct,
    posterior,
    predict_correct,
)
from .protocol import InMemorySkillRepository, SkillRepository
from .service import MAX_EVIDENCE, MIN_CONFIDENCE, SkillService, unreviewed

__all__ = [
    "DEFAULT_CORRECT_THRESHOLD",
    "MAX_EVIDENCE",
    "MIN_CONFIDENCE",
    "BktParameters",
    "InMemorySkillRepository",
    "SkillRepository",
    "SkillService",
    "is_correct",
    "posterior",
    "predict_correct",
    "unreviewed",
]
