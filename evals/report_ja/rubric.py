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
# **ルーブリックはデータセットと別に選べる。** ある年度のコメントから起こした
# ルーブリックを、別の年度の提出に当てて汎化を測るため（転移テスト）。
# 指定しなければデータセットと同じものを使う。
ENV_RUBRIC = "AIJUDGE_EVAL_RUBRIC"
DEFAULT_DATASET = "2025"

_dataset = os.environ.get(ENV_DATASET) or DEFAULT_DATASET
_name = os.environ.get(ENV_RUBRIC) or _dataset
try:
    _module = importlib.import_module(f"rubric_{_name}")
except ModuleNotFoundError as exc:  # pragma: no cover - 打ち間違い
    raise SystemExit(
        f"{ENV_DATASET}={_name!r} に対応する rubric_{_name}.py がありません"
    ) from exc

# ルーブリックの中身をそのまま通す。
DATASET = _dataset
RUBRIC = _module.DATASET
# **段階の記述の出どころはルーブリック側の性質**（データセット側ではない）。
DESCRIPTOR_SOURCE = _module.DESCRIPTOR_SOURCE
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
# 提出物と採点表は**データセット側**から取る（ルーブリックは記述だけ差し替える）。
_source = importlib.import_module(f"rubric_{_dataset}")
SOURCE_DIR = DESIGN_DIR / _source.SOURCE
HUMAN_CSV = DESIGN_DIR / _source.HUMAN_CSV
# **採点表を誰が付けたかはデータセット側の性質**（ルーブリックを差し替えても
# 変わらない）。報告書の冒頭がこれで決まる ── 一致度なのか精度なのか。
SUBJECT = _source.SUBJECT
HUMAN_LABEL = _source.HUMAN_LABEL
HUMAN_IS_INSTRUCTOR = _source.HUMAN_IS_INSTRUCTOR
HUMAN_NOTE = _source.HUMAN_NOTE
STUDENT_RE = _source.STUDENT_RE
load_human = _source.load_human

# 作業用の置き場所。**提出物はデータセット側なので、抽出物はそちらに置く。**
# 採点結果だけはルーブリックごとに名前を分ける（--name で付ける）。
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
