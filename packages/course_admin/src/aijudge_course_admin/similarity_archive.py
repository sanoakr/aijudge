"""過年度のレポートの束を取り込み、提出の類似の比べる相手にする（#653）。

過年度のレポートは aiJudge の外にある（運用は 2026 後期から）。年度ごとの PDF の束を
管理用のコマンド（`aijudge-admin similarity archive import`）で取り込み、課題の
`similarity_references` に `{archive: <名前>}` と書いて比べる。

- **取り出した本文だけを置く。** PDF そのものは置かない。抽出は採点と同じ `document_text`
  （別に実装すると、片方だけが壊れた PDF を読める差が出る）
- 置き場所は `AIJUDGE_SIMILARITY_DIR/archives/<名前>/`。提出の報告と同じく**バックアップ
  しない**。消すのは管理用のコマンド（`archive delete`）で、消すと比べた報告も次の周回で消える
- 画面には束の**ラベル（「2025 年度 ネットワーク レポート 1」など）と文書の番号**しか出さない。
  本文も、元のファイル名も出さない（ファイル名に学籍番号が入っていることが多い）
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from aijudge_core import ArtifactKind

from .code_similarity import ARCHIVE_DIR

logger = logging.getLogger(__name__)

MANIFEST_FILE = "manifest.json"
TEXT_DIR = "texts"
#: 束の名前。パスの部品になるので、英小文字・数字・`.`・`_`・`-` だけにする。
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
#: 取り込む形式。Markdown・LaTeX はそのまま、PDF・docx は本文を取り出す。
SUFFIXES = {
    ".pdf": ArtifactKind.PDF,
    ".docx": ArtifactKind.DOCX,
    ".md": ArtifactKind.MARKDOWN,
    ".tex": ArtifactKind.LATEX,
}


class ArchiveDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 文書の番号（`D-001`）。画面に出るのはこれとラベルだけ。
    id: str
    content_hash: str
    #: 元のファイル名。**画面には出さない**（学籍番号が入っていることが多い）。運用者が
    #: 取り込んだものを確かめるためだけに残す。
    filename: str


class ArchiveManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str
    imported_at: datetime
    documents: tuple[ArchiveDocument, ...] = ()
    #: 本文を取り出せなかったファイル（文字が埋め込まれていない PDF など）。
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedDocument:
    document: ArchiveDocument
    text: str


class ArchiveError(Exception):
    """束を取り込めない・見つからない。"""


def archive_root(root: Path) -> Path:
    return root / ARCHIVE_DIR


def _directory(root: Path, name: str) -> Path:
    if not NAME_PATTERN.fullmatch(name):
        raise ArchiveError(f"束の名前 {name!r} が使えません（英小文字・数字・. _ - で 64 字まで）")
    return archive_root(root) / name


def _text_of(path: Path, kind: ArtifactKind) -> str | None:
    payload = path.read_bytes()
    if not kind.is_document:
        return payload.decode("utf-8", errors="replace")
    from aijudge_ext_document_text import DocumentTextError, text_of

    try:
        return text_of(payload, kind)
    except DocumentTextError as exc:
        logger.info("本文を取り出せませんでした: %s（%s）", path.name, exc)
        return None


def import_archive(
    root: Path, *, name: str, label: str, source: Path, now: datetime | None = None
) -> ArchiveManifest:
    """フォルダの中の文書（下の階層も含む）を取り込む。**同じ名前の束があれば断る。**

    入れ直すときは、先に `delete_archive` で消す（比べた報告も消えて作り直される）。
    """
    target = _directory(root, name)
    if target.exists():
        raise ArchiveError(f"束 {name!r} はもうあります。入れ直すなら先に消してください")
    if not label.strip():
        raise ArchiveError("ラベル（画面に出る出どころ）を書いてください")
    files = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES)
    if not files:
        raise ArchiveError(f"{source} に PDF・docx・Markdown・LaTeX がありません")
    texts = target / TEXT_DIR
    texts.mkdir(parents=True)
    documents: list[ArchiveDocument] = []
    skipped: list[str] = []
    try:
        for path in files:
            text = _text_of(path, SUFFIXES[path.suffix.lower()])
            if text is None:
                skipped.append(str(path.relative_to(source)))
                continue
            document_id = f"D-{len(documents) + 1:03d}"
            (texts / f"{document_id}.txt").write_text(text, encoding="utf-8")
            documents.append(
                ArchiveDocument(
                    id=document_id,
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    filename=str(path.relative_to(source)),
                )
            )
        manifest = ArchiveManifest(
            name=name,
            label=label.strip(),
            imported_at=now or datetime.now(UTC),
            documents=tuple(documents),
            skipped=tuple(skipped),
        )
        (target / MANIFEST_FILE).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return manifest


def read_manifest(root: Path, name: str) -> ArchiveManifest | None:
    try:
        path = _directory(root, name) / MANIFEST_FILE
        return ArchiveManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (ArchiveError, OSError, ValueError):
        return None


def list_archives(root: Path) -> list[ArchiveManifest]:
    base = archive_root(root)
    if not base.is_dir():
        return []
    manifests = (read_manifest(root, p.name) for p in sorted(base.iterdir()) if p.is_dir())
    return [m for m in manifests if m is not None]


def delete_archive(root: Path, name: str) -> bool:
    target = _directory(root, name)
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def load_archive(root: Path, name: str) -> tuple[ArchiveManifest, list[LoadedDocument]] | None:
    manifest = read_manifest(root, name)
    if manifest is None:
        return None
    texts = _directory(root, name) / TEXT_DIR
    loaded: list[LoadedDocument] = []
    for document in manifest.documents:
        try:
            text = (texts / f"{document.id}.txt").read_text(encoding="utf-8")
        except OSError:
            continue
        loaded.append(LoadedDocument(document=document, text=text))
    return manifest, loaded


def archive_hashes(root: Path, name: str) -> dict[str, str]:
    """文書の番号 → 本文のハッシュ。束が無ければ空（比べた報告は消える）。"""
    manifest = read_manifest(root, name)
    return {} if manifest is None else {d.id: d.content_hash for d in manifest.documents}


def manifest_json(manifest: ArchiveManifest) -> str:
    """CLI に出す要約（元のファイル名は出さない）。"""
    return json.dumps(
        {
            "name": manifest.name,
            "label": manifest.label,
            "documents": len(manifest.documents),
            "skipped": len(manifest.skipped),
        },
        ensure_ascii=False,
    )
