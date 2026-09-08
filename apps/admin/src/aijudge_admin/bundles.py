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
from aijudge_authoring import images as image_module

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


# --------------------------------------------------------------------------
# ひな形（#171）
# --------------------------------------------------------------------------
#
# **読む側と同じモジュールに置く。** 構造の定義（上の定数と docstring）を
# 2 か所に置くと、構造を変えた日にひな形だけが古くなり、しかもそれは
# 「取り込めません」の 1 行として教員に返る。落としたひな形がそのまま
# `read_bundle` を通ることをテストで固定してある。


def template_bundle(
    *,
    unit: str = "",
    evaluators: tuple[str, ...] = (),
    criterion_codes: tuple[str, ...] = (),
    kc_keys: tuple[str, ...] = (),
) -> bytes:
    """アップロードするファイル一式のひな形（zip）。

    **コースに合わせて作る。** `task.yaml` のコメントに、そのコースで実際に
    使える値を入れる ── 使える評価器、共通ルーブリックの観点コード、
    このコースが使う知識要素の正準キー。知識要素は登録済みのものしか名指し
    できず（`kc.assert_registered`）、キーの形も決まっている（半角英小文字・
    数字・下線と `.`・#157）ので、一覧が手元にあるかどうかで書きやすさが
    変わる。

    **2 問入れる。** `p1` は最小（`statement` だけ）、`p2` はテストケース・
    参照解答・画像つき。片方だけだと「省略してよいのはどれか」が分からない。
    """
    entries: dict[str, bytes | str] = {
        f"p1/{SPEC_NAME}": _minimal_spec(kc_keys),
        f"p2/{SPEC_NAME}": _full_spec(evaluators, criterion_codes, kc_keys),
        f"p2/{STATEMENT_NAME}": _statement(),
        "p2/reference.c": _REFERENCE_C,
        # **2 件入れる。** 「複数置ける」と README に書くだけでなく、
        # 対の付け方が目に見える形で入っている方が早い（#177）。
        f"p2/{TESTS_DIR}/case1.in": "3 4\n",
        f"p2/{TESTS_DIR}/case1.out": "7\n",
        f"p2/{TESTS_DIR}/case2.in": "-2 5\n",
        f"p2/{TESTS_DIR}/case2.out": "3\n",
        f"p2/{IMAGES_DIR}/fig1.png": _placeholder_png(),
        "README.md": _readme(unit),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(
                name, payload if isinstance(payload, bytes) else payload.encode("utf-8")
            )
    return buffer.getvalue()


def _listing(values: tuple[str, ...], empty: str) -> str:
    """コメントに埋める一覧。**空のときは空欄にしない** ── 空欄は「まだ無い」
    のか「欄が壊れている」のか読めない。
    """
    if not values:
        return f"#   （{empty}）"
    return "\n".join(f"#   {value}" for value in values)


def _minimal_spec(kc_keys: tuple[str, ...]) -> str:
    """最小の `task.yaml`。**必須は `statement` だけである。**"""
    return f"""# 課題 1 件の宣言。必須は statement だけです（詳しくは README.md）。
# 鍵（key）は書きません —— 書いても捨てられます。

statement: |
  ## [必須] 二数の和 ##

  2 つの整数を読み、その和を出力してください。

# title: 二数の和          # 省略すると本文の見出しから採ります
# max_score: 100.0
# position: 1              # 問題セットの中での順序（p1, p2, … の 1, 2, …）

# 知識要素（正準キー）。**登録済みのものしか名指しできません。**
# このコースで使えるのは:
{_listing(kc_keys, "まだありません。コースの「知識要素」で追加してください")}
# knowledge_components:
#   - {kc_keys[0] if kc_keys else "cs.loops.termination"}
"""


def _full_spec(
    evaluators: tuple[str, ...], criterion_codes: tuple[str, ...], kc_keys: tuple[str, ...]
) -> str:
    """テストケース・参照解答・画像つきの `task.yaml`。"""
    codes = _listing(criterion_codes, "宣言していません。組み込みの既定を使います")
    return f"""# 側のファイル（statement.md・reference.c・tests/・images/）を使う例。
# それぞれの役割と書式は README.md にあります。

statement: |
  statement.md があるので、こちらは使われません。

title: 二数の和（図つき）
position: 2

# 観点。**書かなければコースの共通ルーブリックを引き継ぎます。**
# このコースの共通ルーブリックの観点コード:
{codes}
# 書く場合は重みの合計を 1.0 にします（段階は 2 つ以上・最上位を 1.0 に）。
# criteria:
#   - code: correctness
#     title: 出力の正しさ
#     description: 仕様どおりの出力を返すか。
#     weight: 1.0
#     evaluator: {evaluators[0] if evaluators else "code_test_runner"}
#     levels:
#       - {{ level: 0, label: 未達, descriptor: ほとんど正しく動作しない, score_ratio: 0.0 }}
#       - {{ level: 1, label: 達成, descriptor: すべてのケースで正しい, score_ratio: 1.0 }}
#
# この科目で使える評価器:
{_listing(evaluators, "決定的評価器の宣言がありません")}

# knowledge_components:
#   - {kc_keys[0] if kc_keys else "cs.loops.termination"}
"""


def _statement() -> str:
    return """## [必須] 二数の和 ##

2 つの整数を読み、その和を出力してください。

![図](images/fig1.png)

### 入力 ###

空白区切りの整数 2 つ。

### 出力 ###

和を 1 行で。
"""


_REFERENCE_C = """#include <stdio.h>

int main(void) {
    int a, b;
    if (scanf("%d %d", &a, &b) != 2) return 1;
    printf("%d\\n", a + b);
    return 0;
}
"""


def _readme(unit: str) -> str:
    """ひな形の説明（Markdown・#177）。

    **これだけ読めば書けるところまで書く。** 手順しか書いていなかったので、
    ファイルごとの書式は `task.yaml` のコメントに散っており、2 つのファイルを
    行き来しないと全体が掴めなかった。分担は「README が全体、`task.yaml` の
    コメントはその行の意味」で、**同じことを 2 か所に書かない**。

    **上限や拡張子はコードの定数から埋める。** 書き写すと、変えた日に
    README だけが古い数字を出す。
    """
    where = f"「{unit}」" if unit else "問題セット"
    mb = MAX_ARCHIVE_BYTES // (1024 * 1024)
    extracted_mb = MAX_EXTRACTED_BYTES // (1024 * 1024)
    image_mb = image_module.MAX_BYTES // (1024 * 1024)
    image_suffixes = " ".join(f"`{suffix}`" for suffix in sorted(image_module.SUFFIX_TYPES))
    references = " / ".join(f"`{suffix}`" for suffix in REFERENCE_SUFFIXES)
    return f"""# 問題セットの取り込み — ファイル一式の書き方

このひな形を書き換えて zip にすると、{where}のページの
「zip でまとめて取り込む」から取り込めます。

## 手順

1. `p1` / `p2` のフォルダを、作りたい課題の数だけ用意します（1 問でもかまいません）
2. `{SPEC_NAME}` を書き換えます。**必須は `statement` だけ**です
3. フォルダ全体を zip にして選びます
4. 読み取った内容が確認画面に出ます。**そこまでは保存されません**

この `README.md` は取り込みで無視されます（残したままでかまいません）。

## フォルダの決まり

- **`{SPEC_NAME}` のある階層が課題 1 件**で、**フォルダ名がその課題の鍵**になります
- `{SPEC_NAME}` を zip の直下に置くと断ります（課題ごとのフォルダに入れてください）
- 同じ名前のフォルダが 2 つあると断ります
- 鍵の前半（どの問題セットか）は**取り込む画面が決めます**。`{SPEC_NAME}` に
  `key` を書いても捨てます —— フォルダ名の打ち間違いが別の課題に化けるのを
  防ぐためです
- 関係のないファイル（この README や `__MACOSX`）は無視します

## ファイルの役割と書式

### `{SPEC_NAME}`（必須）

課題の宣言です。**知らないキーを書くとその場で断ります**（綴り間違いを
黙って無視しません）。

| 書くもの | 既定・決まり |
|---|---|
| `statement` | **必須。** 課題文（Markdown） |
| `title` | 省略すると本文の見出しから採ります |
| `max_score` | 既定 100.0 |
| `position` | 問題セットの中での順序（`p1`, `p2`, … の 1, 2, …） |
| `criteria` | 省略するとコースの共通ルーブリックを引き継ぎます |
| `knowledge_components` | **登録済みの正準キーだけ**。半角英小文字・数字・下線と `.` |
| `test_cases` | `{TESTS_DIR}/` を置く場合は書きません（下記） |

`criteria` を書く場合は、**重みの合計を 1.0** にし、段階は 2 つ以上、
最上位の `score_ratio` を 1.0 にします。`criteria` と `readability_weight` は
併記できません（どちらが効くのか読めなくなるため）。

### `{STATEMENT_NAME}`（任意）

課題文。**あれば `{SPEC_NAME}` の `statement` より優先します。** 長い本文を
YAML に埋めなくて済ませるためのものです。

### `reference.<拡張子>`（任意）

参照解答。{references} の順で、最初に見つかった 1 つを読みます。

### `{TESTS_DIR}/`（任意） — 入出力のセット

- `<名前>.in` と `<名前>.out` を**対で**置きます。**片方だけだと断ります**
  （期待出力の無いテストケースは、通ったのか確かめていないのか区別が
  付かないまま採点に使われるためです）
- **複数置けます。** 同じ `{TESTS_DIR}/` の中に並べてください:

```
{TESTS_DIR}/case1.in
{TESTS_DIR}/case1.out
{TESTS_DIR}/case2.in
{TESTS_DIR}/case2.out
{TESTS_DIR}/big-input.in
{TESTS_DIR}/big-input.out
```

- 拡張子を除いた部分（`case1`・`big-input`）が**テストケース名**になり、
  採点結果の画面にその名前で出ます。並ぶ順は**名前順**です
- ファイルで置いたケースは**学習者に中身を見せません**。重みはどれも同じ
  （1.0）です
- **`{TESTS_DIR}/` を置くと、`{SPEC_NAME}` の `test_cases` は無視されます。**
  見せるケース（例題）や、重みを変えたいケースがある場合は、`{TESTS_DIR}/` を
  使わず `{SPEC_NAME}` の `test_cases` に書いてください

### `{IMAGES_DIR}/`（任意）

課題文に貼る画像です。課題文から `{IMAGES_DIR}/<名前>` で参照すると、
取り込みのときに正しい URL へ書き換えます（外部の URL を書かないでください
—— 課題文を開くたびに学外へ取りに行くことになります）。

使える形式は {image_suffixes}、1 枚 {image_mb}MB までです。

## 上限

- zip 全体で {mb}MB
- 項目 {MAX_ARCHIVE_ENTRIES} 件
- 展開後 {extracted_mb}MB

## 取り込んだあとの扱い

- **既にある鍵の課題は上書きしません。版が上がります。** 確認画面が
  1 件ずつ「新規 / 変化なし / 版が上がる」を出します
- 既定は**未承認**です。承認するまで学習者には出ません（「未承認の課題」から
  中身を確かめて承認してください）
- 出題済みの版は書き換わらないので、**過去の採点がどの基準で付いたのかは
  そのまま辿れます**
"""


def _placeholder_png() -> bytes:
    """差し替え用の小さな PNG。**中身のある画像を 1 枚入れておく。**

    空のフォルダは zip に残らないので、`images/` の置き場所を示すには
    ファイルが 1 つ要る。
    """
    import struct
    import zlib

    width = height = 16
    raw = b"".join(b"\x00" + bytes([220, 220, 220] * width) for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_ENTRIES",
    "BundledImage",
    "BundledTask",
    "read_bundle",
    "template_bundle",
]
