"""フッターに出す版と著作権表示。"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path


def read_app_version() -> str:
    """release-tagging（ルート pyproject の version、`v<version>` タグ）を読む。

    デプロイは `git checkout --detach vX.Y.Z` した作業木からそのまま起動する
    ので、リポジトリルートの `pyproject.toml` がデプロイ済みタグを表す。
    各パッケージの `pyproject.toml` 自身にも `version` はあるが、
    こちらは `0.0.1` に固定されたプレースホルダで運用しない（`name` で見分ける）。
    フッターの表示を壊す理由にはならないので、読めなければ "unknown" とする。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            data = tomllib.loads(candidate.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project")
        if isinstance(project, dict) and project.get("name") == "aijudge":
            version = project.get("version")
            if isinstance(version, str):
                return version
    return "unknown"


def read_copyright_notice() -> str:
    """`LICENSE` の Copyright 行を読んで著作権表示を作る（#145）。

    表記を手で書き写すと `LICENSE` と footer がいずれずれる。
    開始年は `LICENSE` の記載のまま、終了年は表示時点の年（同じなら 1 年だけ
    出す）。読めなければ空文字を返す（フッターの他の表示を道連れにしない）。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "LICENSE"
        if not candidate.is_file():
            continue
        match = re.search(
            r"^\s*Copyright\s+(\d{4})\s+(.+?)\s*$", candidate.read_text(), re.MULTILINE
        )
        if match is None:
            return ""
        start_year, holder = match.group(1), match.group(2)
        current_year = str(datetime.now(UTC).year)
        years = start_year if start_year == current_year else f"{start_year}–{current_year}"
        return f"© {years} {holder}"
    return ""
