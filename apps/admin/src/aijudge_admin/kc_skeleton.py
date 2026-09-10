"""KC の骨格の読み込みと投入（`subjects/kc/*.yaml`）。

**骨格は分野（第 1 階層）と単位（第 2 階層）を決める。** そこは CS2023 の
Knowledge Area / Knowledge Unit そのままで、画面からは足せない ── 足せる
ようにした瞬間に `cs.loops` と `cs.iteration` が並ぶ（`kc.py` の規則 1 と
同じ理由で、これはコードと同じレビューを通すべき決定である）。

**第 3 階層（知識要素）は推奨候補**として一緒に入る。教員はここに足せるし、
足すときは近いものが提示される ── 骨格が「正解の一覧」ではなく「まず
ここから探す場所」であることが要点で、禁止すると教員は近いキーに無理やり
寄せ、構造としてはより悪くなる（`kc.py` 冒頭の判断と同じ）。

**第 4 階層は無い。** 深さは 3 で止める。細かくしすぎると 1 つの KC に
課題が 1 件しか対応せず、習熟度が推定できない（Q-matrix が薄くなる）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .operations import AdminError

# 分野 . 単位 . 知識要素。**名前空間は階層に数えない。**
MAX_KC_DEPTH = 3


@dataclass(frozen=True)
class SkeletonEntry:
    """骨格の 1 行。`key` は名前空間を含まない相対キー。"""

    key: str
    label: str
    depth: int


@dataclass(frozen=True)
class Skeleton:
    namespace: str
    source: str
    entries: tuple[SkeletonEntry, ...]

    @property
    def areas(self) -> tuple[SkeletonEntry, ...]:
        return tuple(e for e in self.entries if e.depth == 1)

    @property
    def units(self) -> tuple[SkeletonEntry, ...]:
        return tuple(e for e in self.entries if e.depth == 2)

    @property
    def components(self) -> tuple[SkeletonEntry, ...]:
        return tuple(e for e in self.entries if e.depth == 3)


def skeleton_dir(profiles_dir: Path) -> Path:
    """骨格の置き場所。**科目プロファイルと同じ場所の下に置く。**

    運用ではプロファイルが git のチェックアウトの外にあるので
    （`subjects/README.md`）、骨格もそちらに付いていく必要がある ──
    片方だけリポジトリ側を見ると、画面の語彙と投入した語彙が食い違う。
    """
    return profiles_dir / "kc"


def load_skeleton(path: Path) -> Skeleton:
    """骨格ファイルを読む。**形が違えば読まない。**

    黙って一部だけ投入すると、木が半分だけある状態になり、教員には
    「なぜかこの単位だけ無い」としか見えない。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdminError(f"骨格ファイルを読めません: {path}（{exc}）") from None
    except yaml.YAMLError as exc:
        raise AdminError(f"骨格ファイルの形が壊れています: {path}（{exc}）") from None

    if not isinstance(raw, dict) or "namespace" not in raw or "areas" not in raw:
        raise AdminError(f"骨格ファイルに namespace と areas が要ります: {path}")

    namespace = str(raw["namespace"]).strip()
    entries: list[SkeletonEntry] = []
    seen: set[str] = set()

    for area in raw["areas"] or ():
        akey = _segment(area, "area", path)
        _add(entries, seen, akey, _label(area, akey), 1, path)
        for unit in area.get("units") or ():
            ukey = _segment(unit, "unit", path)
            key = f"{akey}.{ukey}"
            _add(entries, seen, key, _label(unit, ukey), 2, path)
            for comp in unit.get("components") or ():
                ckey = _segment(comp, "component", path)
                _add(entries, seen, f"{key}.{ckey}", _label(comp, ckey), 3, path)

    if not entries:
        raise AdminError(f"骨格ファイルが空です: {path}")
    return Skeleton(
        namespace=namespace,
        source=str(raw.get("source") or "").strip(),
        entries=tuple(entries),
    )


def _segment(node: object, what: str, path: Path) -> str:
    if not isinstance(node, dict) or not str(node.get("key") or "").strip():
        raise AdminError(f"{path}: {what} に key がありません（{node!r}）")
    return str(node["key"]).strip()


def _label(node: dict, fallback: str) -> str:
    return str(node.get("label") or "").strip() or fallback


def _add(
    entries: list[SkeletonEntry], seen: set[str], key: str, label: str, depth: int, path: Path
) -> None:
    if key in seen:
        # 骨格の中の重複は、投入してから気づくと直せない（ID がキーから
        # 決まるので、後から片方だけ消せない）。読む段階で落とす。
        raise AdminError(f"{path}: キーが重複しています: {key!r}")
    seen.add(key)
    entries.append(SkeletonEntry(key=key, label=label, depth=depth))
