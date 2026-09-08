"""課題の束（zip）を読んで `TaskSpec` に変える（#161）。

**受け取る構造はこのシステム自身の語彙である。** 移行元（Sharif Judge）の
ディレクトリ形式をここに持ち込まない ── 一度その形で入口を作り、廃止した
（`0231d94`）。理由は形式が移行より長く残ることと、その経路だけが採点の
意味を勝手に変えていたこと（`readability_weight` を 0.0 に固定していたので、
画面から入れた課題にだけ AI 観点が付かなかった）。

    ex06.zip
    ├── p1/
    │   ├── task.yaml        TaskSpec そのもの（フィールド名も同じ）
    │   ├── statement.md     任意。task.yaml の statement を上書き
    │   ├── reference.c      任意。reference_solution に読み込む
    │   ├── tests/
    │   │   ├── case1.in     任意。test_cases[].input / .expected に読み込む
    │   │   └── case1.out
    │   └── images/fig1.png  任意。呼び出し側が画像ストアへ入れる
    └── p2/task.yaml

側のファイルは**長い文字列を YAML に埋めなくて済むための糖衣**にすぎない。
模型は増やさない ── 読み終えた時点で、あとは `TaskSpec` が 1 つあるだけ。

**ここでは何も保存しない。** 読んで、何が起きるかを言うだけ。保存は
`aijudge_admin.authoring.save_task`（画面・API・CLI と同じ経路）が行う。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from aijudge_authoring import TaskSpec

from .operations import AdminError

# 束の上限。課題はテキストとテストケースなので小さい。大きいものは事故か攻撃
# （廃止された zip 取り込みの値をそのまま使う）。
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
# 展開後の上限。圧縮爆弾は、小さな zip が展開でディスクを埋める。
MAX_EXTRACTED_BYTES = MAX_ARCHIVE_BYTES * 8

SPEC_NAME = "task.yaml"
STATEMENT_NAME = "statement.md"
TESTS_DIR = "tests"
IMAGES_DIR = "images"
# 参照解答として読む拡張子。**提出の受付形式とは別の表**
# （こちらは「教員が置く正解」で、学習者が出せるものとは一致しない）。
REFERENCE_SUFFIXES = (".c", ".py", ".java", ".txt")


@dataclass(frozen=True)
class BundledImage:
    """課題に添えられた画像。**まだどこにも保存していない。**"""

    name: str
    payload: bytes


@dataclass(frozen=True)
class BundledTask:
    """束から読んだ課題 1 件。"""

    leaf: str
    spec: TaskSpec
    images: tuple[BundledImage, ...] = field(default_factory=tuple)


def read_bundle(payload: bytes) -> tuple[BundledTask, ...]:
    """zip を読んで課題を並べる。**保存はしない。**

    1 件でも読めなければ全体を断る ── 半分だけ入った状態は、教員が
    「何が入って何が入らなかったか」を数え直すことになる。
    """
    if not payload:
        raise AdminError("ファイルが空です")
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise AdminError(f"zip が大きすぎます（上限 {MAX_ARCHIVE_BYTES} バイト）")

    files = _extract(payload)
    leaves = _leaves(files)
    if not leaves:
        raise AdminError(
            f"課題が 1 件も見つかりません（各課題のフォルダに {SPEC_NAME} を置いてください）"
        )
    return tuple(_read_task(leaf, files) for leaf in leaves)


def _extract(payload: bytes) -> dict[str, bytes]:
    """zip を**メモリ上で**読む。展開先を作らないので zip slip が起きない。

    それでも項目名は検査する ── 名前は攻撃者が決められるので、`../` や
    絶対パスをそのまま鍵に使うと、後段（画像の保存など）で外に出る。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise AdminError("zip として読めません") from exc

    infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise AdminError(f"項目が多すぎます（上限 {MAX_ARCHIVE_ENTRIES}）")

    total = 0
    files: dict[str, bytes] = {}
    for info in infos:
        name = info.filename
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts:
            raise AdminError(f"zip に不正なパスが含まれています: {name!r}")
        if parts and parts[0] == "__MACOSX":
            continue
        # 展開後の大きさも見る（圧縮爆弾）。
        total += info.file_size
        if total > MAX_EXTRACTED_BYTES:
            raise AdminError("展開後のサイズが大きすぎます")
        files[name] = archive.read(info)
    return files


def _leaves(files: dict[str, bytes]) -> tuple[str, ...]:
    """`task.yaml` のある階層を課題 1 件として拾う。"""
    leaves = []
    for name in sorted(files):
        path = PurePosixPath(name)
        if path.name != SPEC_NAME:
            continue
        parent = path.parent
        if not parent.parts:
            raise AdminError(
                f"{SPEC_NAME} が zip の直下にあります"
                "（課題ごとのフォルダに入れてください: `p1/task.yaml`）"
            )
        leaves.append(parent.parts[-1])
    duplicates = {leaf for leaf in leaves if leaves.count(leaf) > 1}
    if duplicates:
        raise AdminError(f"同じ名前の課題フォルダが複数あります: {sorted(duplicates)}")
    return tuple(leaves)


def _read_task(leaf: str, files: dict[str, bytes]) -> BundledTask:
    """1 件ぶんを読む。**綴り間違いはここで落ちる**（`extra="forbid"`）。"""
    prefix = _prefix_of(leaf, files)
    data = _yaml(files[f"{prefix}{SPEC_NAME}"], f"{leaf}/{SPEC_NAME}")

    statement = files.get(f"{prefix}{STATEMENT_NAME}")
    if statement is not None:
        data["statement"] = _text(statement, f"{leaf}/{STATEMENT_NAME}")

    reference = _reference(prefix, files, leaf)
    if reference is not None:
        data["reference_solution"] = reference

    cases = _test_cases(prefix, files, leaf)
    if cases:
        data["test_cases"] = cases

    # 鍵は画面が決める（`#70`・問題セットの取り違えを避ける）。zip 側の
    # 指定は受け取らない ── 受け取ると、フォルダ名の打ち間違いが別の課題に化ける。
    data.pop("key", None)
    data.setdefault("key", leaf)

    try:
        spec = TaskSpec.model_validate(data)
    except Exception as exc:
        raise AdminError(f"{leaf}/{SPEC_NAME}: {exc}") from None
    return BundledTask(leaf=leaf, spec=spec, images=_images(prefix, files))


def _prefix_of(leaf: str, files: dict[str, bytes]) -> str:
    for name in files:
        path = PurePosixPath(name)
        if path.name == SPEC_NAME and path.parent.parts and path.parent.parts[-1] == leaf:
            return f"{path.parent}/"
    raise AdminError(f"{leaf}: {SPEC_NAME} が見つかりません")  # pragma: no cover - 上流で保証


def _yaml(payload: bytes, where: str) -> dict:
    try:
        data = yaml.safe_load(_text(payload, where))
    except yaml.YAMLError as exc:
        raise AdminError(f"{where}: YAML として読めません: {exc}") from None
    if not isinstance(data, dict):
        raise AdminError(f"{where}: 最上位は対応表（キー: 値）である必要があります")
    return data


def _text(payload: bytes, where: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise AdminError(f"{where}: UTF-8 として読めません") from None


def _reference(prefix: str, files: dict[str, bytes], leaf: str) -> str | None:
    for suffix in REFERENCE_SUFFIXES:
        name = f"{prefix}reference{suffix}"
        if name in files:
            return _text(files[name], f"{leaf}/reference{suffix}")
    return None


def _test_cases(prefix: str, files: dict[str, bytes], leaf: str) -> list[dict[str, object]]:
    """`tests/<name>.in` と `tests/<name>.out` を対で読む。

    **片方だけあるものは断る。** 期待出力の無いテストケースは、通ったのか
    確かめていないのか区別できないまま採点に使われる。
    """
    root = f"{prefix}{TESTS_DIR}/"
    inputs = sorted(name for name in files if name.startswith(root) and name.endswith(".in"))
    cases: list[dict[str, object]] = []
    for name in inputs:
        stem = PurePosixPath(name).stem
        expected = f"{root}{stem}.out"
        if expected not in files:
            raise AdminError(f"{leaf}/{TESTS_DIR}/{stem}.in に対応する {stem}.out がありません")
        cases.append(
            {
                "name": stem,
                "input": _text(files[name], name),
                "expected": _text(files[expected], expected),
            }
        )
    orphans = sorted(
        PurePosixPath(name).stem
        for name in files
        if name.startswith(root)
        and name.endswith(".out")
        and f"{root}{PurePosixPath(name).stem}.in" not in files
    )
    if orphans:
        raise AdminError(f"{leaf}/{TESTS_DIR}: 入力の無い期待出力があります: {orphans}")
    return cases


def _images(prefix: str, files: dict[str, bytes]) -> tuple[BundledImage, ...]:
    root = f"{prefix}{IMAGES_DIR}/"
    return tuple(
        BundledImage(name=PurePosixPath(name).name, payload=files[name])
        for name in sorted(files)
        if name.startswith(root)
    )


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_ENTRIES",
    "BundledImage",
    "BundledTask",
    "read_bundle",
]
