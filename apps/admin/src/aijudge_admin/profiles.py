"""科目プロファイル（`*.yaml`）のファイル操作（#146）。

**参照されているプロファイルは書き換えない。** 1 つのプロファイルは複数の
コースの雛形になりうるので、書き換えると自分が担当していないコースの採点まで
変わる ── ADR 0002 が「プロファイルはコードと同じ扱いでレビューを通す」と
言っているのはこの範囲のこと。

そこで編集できる範囲を絞る（#146 の 2026-09-06 決定）。

- どのコースからも参照されていないプロファイル ── 直接編集・改名できる
- 1 コースでも参照しているプロファイル ── 読み取り専用。唯一の操作は
  **複製して編集**（複製先は未参照なので自由に編集できる。既存のコースは
  何もしなければ元のプロファイルを参照し続ける）

**複製は模型を経由しない。** ファイルをそのまま写して `name` の行だけ
差し替える ── `subjects/*.yaml` は行数の大半が「なぜその値なのか」を書いた
コメントで（実測値・過去の事故・ADR 番号）、`yaml.dump` で書き戻すとそれが
全部消える。値だけ残っても、次に誰かがその値を変えるときに理由を失う。

書き込みは一時ファイル + `os.replace` で原子的に行う。ワーカー・web・
コンソールが同じディレクトリを**別プロセスから読んでいる**ので、途中まで
書けたファイルを読ませない。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from aijudge_core import Course
from aijudge_grading import EvaluatorRegistry, SubjectProfile

from .operations import AdminError

# プロファイル名に許す形。ファイル名になるので、パス区切りや空白は入れない。
# 既存の名前（`cs_lang_c_intro`・`report_ja`）と同じ書き方に揃える。
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


@dataclass(frozen=True)
class ProfileSummary:
    """一覧に出す 1 件。**なぜ編集できないかを持つ。**

    「参照されている」ことと「編集できない」ことを別の値にしない ──
    画面が理由を書けないと、教員は権限の問題かバグかを区別できない。
    """

    name: str
    description: str | None
    used_by: tuple[Course, ...]

    @property
    def editable(self) -> bool:
        return not self.used_by


def profile_path(name: str, profiles_dir: Path) -> Path:
    """名前からファイルの場所。**名前を検査してから使う。**

    検査せずに繋ぐと `../` でディレクトリの外に書ける。
    """
    if not NAME_PATTERN.match(name):
        raise AdminError(
            f"プロファイル名 {name!r} は使えません"
            "（英小文字で始まり、英小文字・数字・下線だけ・2〜63 文字）"
        )
    return profiles_dir / f"{name}.yaml"


def list_profiles(
    profiles_dir: Path, used_by: dict[str, tuple[Course, ...]]
) -> tuple[ProfileSummary, ...]:
    """ディレクトリの全プロファイルを、参照コースつきで並べる。

    **壊れているファイルも一覧には出す。** 読めないから隠すと、運用者は
    「置いたはずのものが無い」を追いかけることになる。説明が読めない場合は
    `description` を None にする。
    """
    summaries: list[ProfileSummary] = []
    for path in sorted(profiles_dir.glob("*.yaml")):
        name = path.stem
        summaries.append(
            ProfileSummary(
                name=name,
                description=_description_of(path),
                used_by=used_by.get(name, ()),
            )
        )
    return tuple(summaries)


def read_profile_text(name: str, profiles_dir: Path) -> str:
    """YAML の**全文**を返す（コメントを含む）。編集画面に出すもの。"""
    path = profile_path(name, profiles_dir)
    if not path.is_file():
        raise AdminError(f"プロファイル {name!r} がありません")
    return path.read_text(encoding="utf-8")


def save_profile_text(
    name: str,
    text: str,
    *,
    profiles_dir: Path,
    registry: EvaluatorRegistry,
    used_by: tuple[Course, ...],
) -> None:
    """編集した全文を保存する。**参照されていれば拒否する。**

    保存の前に模型の検証と評価器の実在検査を通す（起動時と同じ検査）。
    通らないものを書くと、次の採点が科目ごと止まる。
    """
    if used_by:
        raise AdminError(
            f"プロファイル {name!r} は {len(used_by)} 件のコースが使っています。"
            "複製して編集してください"
        )
    path = profile_path(name, profiles_dir)
    _validate_text(name, text, registry)
    _write_atomic(path, text)


def duplicate_profile(
    source: str, new_name: str, *, profiles_dir: Path, description: str | None = None
) -> Path:
    """複製する。**ファイルを写して `name` の行だけ差し替える。**

    模型を経由して書き戻さないのは、コメント（なぜその値なのかの記録）を
    失わないため（このモジュールの docstring 参照）。
    """
    source_path = profile_path(source, profiles_dir)
    if not source_path.is_file():
        raise AdminError(f"複製元 {source!r} がありません")
    target = profile_path(new_name, profiles_dir)
    if target.exists():
        raise AdminError(f"プロファイル {new_name!r} は既にあります")

    text = _replace_scalar(source_path.read_text(encoding="utf-8"), "name", new_name)
    if description is not None:
        text = _replace_scalar(text, "description", description)
    _write_atomic(target, text)
    return target


def rename_profile(
    name: str, new_name: str, *, profiles_dir: Path, used_by: tuple[Course, ...]
) -> Path:
    """改名する。**参照されていれば拒否する。**

    参照しているコースは名前で雛形を指している（`Course.subject_profile`）。
    改名すると、そのコースは存在しない雛形を指すことになり、採点が
    「雛形がありません」で止まる。名前を変えたいなら複製して新しい名前を選ぶ。
    """
    if used_by:
        raise AdminError(
            f"プロファイル {name!r} は {len(used_by)} 件のコースが使っています。"
            "改名すると、そのコースの採点が止まります（複製して編集してください）"
        )
    source = profile_path(name, profiles_dir)
    if not source.is_file():
        raise AdminError(f"プロファイル {name!r} がありません")
    target = profile_path(new_name, profiles_dir)
    if target.exists():
        raise AdminError(f"プロファイル {new_name!r} は既にあります")

    _write_atomic(target, _replace_scalar(source.read_text(encoding="utf-8"), "name", new_name))
    source.unlink()
    return target


def _validate_text(name: str, text: str, registry: EvaluatorRegistry) -> None:
    """保存しようとしている全文が、起動時の検査を通ることを確かめる。"""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AdminError(f"YAML として読めません: {exc}") from None
    if not isinstance(data, dict):
        raise AdminError("YAML の最上位は対応表（キー: 値）である必要があります")
    data.setdefault("name", name)
    if data.get("name") != name:
        # ファイル名と `name` がずれると、`load_profiles` が引く名前と
        # コースが指す名前が食い違う。
        raise AdminError(f"`name` は {name!r} である必要があります（ファイル名と揃える）")
    try:
        profile = SubjectProfile.model_validate(data)
        profile.validate_against(registry)
    except Exception as exc:
        raise AdminError(f"設定として通りません: {exc}") from None


def _description_of(path: Path) -> str | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("description")
    return value if isinstance(value, str) else None


def _replace_scalar(text: str, key: str, value: str) -> str:
    """`key: 値` の行だけを差し替える（無ければ先頭に足す）。

    **行単位で触る。** 模型に読み込んで書き戻すとコメントが消える。
    """
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f"{key}: {value}"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return f"{replacement}\n{text}"


def _write_atomic(path: Path, text: str) -> None:
    """一時ファイルに書いてから差し替える。

    ワーカー・web・コンソールは同じディレクトリを別プロセスから読む。
    途中まで書けたファイルを読ませると、その科目の採点が止まる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


__all__ = [
    "ProfileSummary",
    "duplicate_profile",
    "list_profiles",
    "profile_path",
    "read_profile_text",
    "rename_profile",
    "save_profile_text",
]
