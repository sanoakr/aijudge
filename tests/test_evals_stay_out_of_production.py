"""測定用のハーネスが本番系に混ざっていないことを機械で保証する。

`evals/report_ja/` はレポート採点の測定のために書いた**使い捨ての道具**で、
ルーブリックの版・採点表の読み込み・年度ごとの照合を持つ。そこで得た知見は
科目プロファイル（`subjects/*.yaml`）と評価器に**書き写して**移す。
**コードは移さない。**

移すと 2 つ壊れる。

1. ADR 0007 の「測定を消しても採点は動く」が成立しなくなる。ハーネスは
   採点表（教員の生の点数）を読む。本番の採点経路がそれを読めるように
   なった時点で、照合先を見ながら採点する経路が存在してしまう。
2. ハーネスは学生の氏名・学籍番号を含むデータを前提に書いてある
   （`.gitignore` の `data-*` 一式）。本番から参照できる位置に置くと、
   個人情報の境界が設計から消える（設計原則 P7）。

`import-linter` では守れない。ハーネスは配布物件（workspace member）では
なく、`sys.path` を書き換えて動く素のスクリプトの集まりなので、契約が参照できる
モジュール名を持たないからである。だから本文を読んで確かめる。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 本番系。ここに測定用ハーネスへの参照があってはならない。
PRODUCTION_TREES = ("packages", "evaluators", "normalizers", "apps")

# `evals` 配下を指す import の書き方。
FORBIDDEN = (
    re.compile(r"^\s*(?:from|import)\s+evals\b", re.MULTILINE),
    re.compile(r"^\s*from\s+report_ja\b", re.MULTILINE),
    re.compile(r"^\s*import\s+(?:rubric|rubric_\d+\w*)\s*$", re.MULTILINE),
    re.compile(r"""sys\.path\.(?:insert|append)\([^)]*evals""", re.MULTILINE),
)


def _production_sources() -> list[Path]:
    return [
        path
        for tree in PRODUCTION_TREES
        for path in (REPO_ROOT / tree).rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    ]


def test_production_code_does_not_import_the_measurement_harness() -> None:
    offenders: list[str] = []
    for path in _production_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")
    assert not offenders, "測定用ハーネスを本番系が参照している:\n" + "\n".join(offenders)


def test_the_harness_is_not_a_distributable_package() -> None:
    """配布物件にしない。

    workspace member や依存に入った瞬間、`uv sync` で本番環境にも入る。
    入ってしまえば「参照していないから安全」は運用の約束でしかなくなる。
    """
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    members = manifest["tool"]["uv"]["workspace"]["members"]
    assert not any(m.startswith("evals") for m in members), members

    # `aijudge-eval-report-structure` は本番の評価器なので別物。
    # 弾きたいのはハーネス（`evals/report_ja/`）そのものである。
    dependencies = manifest["project"]["dependencies"]
    assert not any("report-ja" in d or "aijudge-evals" in d for d in dependencies), dependencies

    assert not (REPO_ROOT / "evals" / "report_ja" / "pyproject.toml").exists(), (
        "ハーネスにパッケージ定義が付いている。付けると配布できてしまう"
    )


def test_the_harness_reads_student_data_only_from_ignored_paths() -> None:
    """採点表と本文の置き場が `.gitignore` に載っていること。

    載っていない置き場が増えると、氏名と学籍番号が追跡対象に入る。
    実際に一度入りかけた（`79f3409`）。
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("evals/report_ja/data-*/", "evals/report_ja/bodies/"):
        assert entry in ignored, f"{entry} が .gitignore に無い"
