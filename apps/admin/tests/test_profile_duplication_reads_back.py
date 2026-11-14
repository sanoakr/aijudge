"""プロファイルの複製・改名で、説明文が YAML を壊さない（2026-11-14・#698）。

説明を `description: <値>` の行にそのまま埋め、しかも `re.sub` の置換文字列として渡していた。
`: ` や `#` や `[` を含む普通の日本語の説明で読めない YAML が保存され、バックスラッシュでは
未捕捉の例外になり、改行を入れると最上位に別のキーを混ぜられた。複製元が折り畳み形式
（`description: >`）だと、新しい説明の後ろに元の説明の続きの行が連結された。

できたファイルを**実際に読み戻して**、名前と説明が入力どおりであることを確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.profiles import duplicate_profile, rename_profile

REPO_ROOT = Path(__file__).resolve().parents[3]

TRICKY = [
    "2026 後期: prog2 用",
    "[draft] copy",
    "C言語 #2 クラス",
    r"regex \d+ と C:\new\dir",
    '二重引用符 " を含む',
    "末尾が - の説明 -",
]


@pytest.fixture
def profiles(tmp_path: Path) -> Path:
    directory = tmp_path / "subjects"
    directory.mkdir()
    (directory / "plain.yaml").write_text(
        "# なぜその値かの記録\nname: plain\ndescription: 元の説明\nreview_policy: {}\n",
        encoding="utf-8",
    )
    (directory / "folded.yaml").write_text(
        "name: folded\ndescription: >\n  折り畳みの 1 行目\n  折り畳みの 2 行目\n\n"
        "  3 行目\nother: 1\n",
        encoding="utf-8",
    )
    return directory


@pytest.mark.parametrize("description", TRICKY)
def test_a_duplicate_reads_back_with_the_description_as_typed(
    profiles: Path, description: str
) -> None:
    target = duplicate_profile("plain", "copy", profiles_dir=profiles, description=description)

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["name"] == "copy"
    assert data["description"] == description
    assert data["review_policy"] == {}, "ほかの設定が動いた"
    assert target.read_text(encoding="utf-8").startswith("# なぜその値かの記録")


def test_a_newline_cannot_inject_a_top_level_key(profiles: Path) -> None:
    target = duplicate_profile(
        "plain", "copy", profiles_dir=profiles, description="x\nreview_policy: {evil: true}"
    )

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["review_policy"] == {}
    assert data["description"] == "x\nreview_policy: {evil: true}"


def test_a_folded_source_description_leaves_no_continuation_lines(profiles: Path) -> None:
    target = duplicate_profile("folded", "copy", profiles_dir=profiles, description="新しい説明")

    text = target.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data == {"name": "copy", "description": "新しい説明", "other": 1}
    assert "折り畳み" not in text


def test_a_rename_keeps_a_single_name_line(profiles: Path) -> None:
    target = rename_profile("plain", "renamed", profiles_dir=profiles, used_by=())

    assert target.read_text(encoding="utf-8").count("name:") == 1
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["name"] == "renamed"


def test_the_real_network_profile_duplicates_cleanly(tmp_path: Path) -> None:
    """折り畳み形式の description を持つ実物の見本で通す。"""
    source = REPO_ROOT / "subjects" / "cs_network_python.yaml"
    directory = tmp_path / "subjects"
    directory.mkdir()
    (directory / "cs_network_python.yaml").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )

    target = duplicate_profile(
        "cs_network_python", "net_copy", profiles_dir=directory, description="複製: 試験用"
    )

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["name"] == "net_copy" and data["description"] == "複製: 試験用"


def test_an_existing_target_is_still_refused(profiles: Path) -> None:
    with pytest.raises(AdminError):
        duplicate_profile("plain", "folded", profiles_dir=profiles)
