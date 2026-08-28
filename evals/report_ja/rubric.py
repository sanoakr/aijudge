"""採点表の読み込み口。**どのデータセットを見るかはここだけが知っている。**

`AIJUDGE_EVAL_DATASET` で切り替える（既定は 2025）。各スクリプトは
`import rubric` のまま変えずに済み、データセットを足す作業は
`rubric_<年>.py` を 1 本書くだけになる。

    AIJUDGE_EVAL_DATASET=2023 uv run python evals/report_ja/extract.py

置き場所もここで決める。**データセットごとに分ける** ── 混ぜると、
2023 年度の採点結果を 2025 年度のルーブリックで読むことになる。
提出物と採点表は repository の外（設計検討ディレクトリ）にある。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

ENV_DATASET = "AIJUDGE_EVAL_DATASET"
DEFAULT_DATASET = "2025"

_name = os.environ.get(ENV_DATASET, DEFAULT_DATASET)
try:
    _module = importlib.import_module(f"rubric_{_name}")
except ModuleNotFoundError as exc:  # pragma: no cover - 打ち間違い
    raise SystemExit(
        f"{ENV_DATASET}={_name!r} に対応する rubric_{_name}.py がありません"
    ) from exc

# ルーブリックの中身をそのまま通す。
DATASET = _module.DATASET
POINTS = _module.POINTS
TOTAL_POINTS = _module.TOTAL_POINTS
MAX_LEVEL = _module.MAX_LEVEL
TOTAL_WEIGHT = _module.TOTAL_WEIGHT
TOTAL_MAX = _module.TOTAL_MAX
HUMAN_COLUMN = _module.HUMAN_COLUMN
STATEMENT = _module.STATEMENT
CRITERIA = _module.CRITERIA
EVALUATOR_OPTIONS = _module.EVALUATOR_OPTIONS
STUDENT_RE = _module.STUDENT_RE
load_human = _module.load_human

CODES = [c["code"] for c in CRITERIA]

# 設計検討ディレクトリ。提出物と採点表はここにある（repository には置かない）。
DESIGN_DIR = Path.home() / "pCloud Drive/Agent Projects/aiJudge設計検討"
SOURCE_DIR = DESIGN_DIR / _module.SOURCE
HUMAN_CSV = DESIGN_DIR / _module.HUMAN_CSV

# 作業用の置き場所。すべて .git/info/exclude で除外してある。
WORK = Path(__file__).parent / f"data-{DATASET}"
INDEX = WORK / "index.json"
BODIES = WORK / "bodies"
BODIES_ANON = WORK / "bodies_anon"
PROMPTS = WORK / "prompts"
RUNS = WORK / "runs"
AGENT_OUT = WORK / "agent_out"


def total(levels: dict[str, int]) -> int | None:
    """全観点そろっているときだけ合計を出す。**欠けを 0 で埋めない。**

    段階と配点が 1 対 1 でない科目があるので、必ずここを通す
    （2023 年度は比率 0〜10 × 配点 5/5/5/10）。
    """
    if any(code not in levels for code in CODES):
        return None
    return sum(levels[code] * TOTAL_WEIGHT[code] for code in CODES)


def ensure_dirs() -> None:
    for path in (WORK, BODIES, BODIES_ANON, PROMPTS, RUNS, AGENT_OUT):
        path.mkdir(parents=True, exist_ok=True)
