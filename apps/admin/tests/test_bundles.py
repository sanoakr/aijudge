"""課題の束（zip）の読み取り（#161）。

固定したいのは 2 つ。

1. **危ないものを断る。** zip の項目名は攻撃者が決められる（zip slip・
   圧縮爆弾・項目数）。廃止された取り込みが持っていた検査をそのまま持つ。
2. **形式はこのシステムの語彙である。** 読み終えた結果は `TaskSpec` で、
   側のファイルは長い文字列を YAML に埋めずに済ませるための糖衣にすぎない。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from aijudge_admin import AdminError
from aijudge_admin.bundles import MAX_ARCHIVE_ENTRIES, read_bundle

MINIMAL = "statement: |\n  ## [必須] 最大値 ##\n\n  本文\n"


def zipped(entries: dict[str, bytes | str], *, compress: bool = False) -> bytes:
    """テスト用の zip を作る。

    既定は無圧縮 ── 中身と項目名だけを見たいテストで、圧縮率に結果が
    左右されないようにする。圧縮爆弾のテストだけが `compress=True` を使う
    （**圧縮しないと、zip 自体の大きさで先に断られて、確かめたい検査に
    届かない**）。
    """
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        for name, payload in entries.items():
            archive.writestr(
                name, payload if isinstance(payload, bytes) else payload.encode("utf-8")
            )
    return buffer.getvalue()


# --------------------------------------------------------------------------
# 危ないものを断る
# --------------------------------------------------------------------------


def test_a_path_escaping_the_archive_is_refused() -> None:
    """zip slip。項目名に `../` を入れると展開先の外に書ける。"""
    with pytest.raises(AdminError, match="不正なパス"):
        read_bundle(zipped({"../escaped/task.yaml": MINIMAL}))


def test_an_absolute_path_is_refused() -> None:
    with pytest.raises(AdminError, match="不正なパス"):
        read_bundle(zipped({"/etc/passwd": "x"}))


def test_a_zip_bomb_is_refused() -> None:
    """小さな zip が展開でディスクを埋める。**展開後の大きさを見る。**"""
    bomb = zipped({"p1/task.yaml": MINIMAL, "p1/big.txt": b"0" * (80 * 1024 * 1024)}, compress=True)
    # zip そのものは上限より小さい（だから大きさの検査は通ってしまう）。
    assert len(bomb) < 8 * 1024 * 1024

    with pytest.raises(AdminError, match="展開後のサイズ"):
        read_bundle(bomb)


def test_too_many_entries_are_refused() -> None:
    entries = {f"p1/tests/case{i}.in": "x" for i in range(MAX_ARCHIVE_ENTRIES + 1)}
    with pytest.raises(AdminError, match="項目が多すぎます"):
        read_bundle(zipped(entries))


def test_something_that_is_not_a_zip_is_refused() -> None:
    with pytest.raises(AdminError, match="zip として読めません"):
        read_bundle(b"not a zip at all")


def test_an_empty_upload_is_refused() -> None:
    with pytest.raises(AdminError, match="空です"):
        read_bundle(b"")


# --------------------------------------------------------------------------
# 読めたものは TaskSpec になる
# --------------------------------------------------------------------------


def test_a_minimal_bundle_becomes_one_spec() -> None:
    (task,) = read_bundle(zipped({"p1/task.yaml": MINIMAL}))

    assert task.leaf == "p1"
    assert "最大値" in task.spec.statement
    # **鍵はフォルダ名から。** zip 側の指定は使わない（画面が前半を決める）。
    assert task.spec.key == "p1"


def test_the_key_in_the_yaml_is_ignored() -> None:
    """フォルダ名の打ち間違いが別の課題に化けるのを避ける（#70）。"""
    (task,) = read_bundle(zipped({"p1/task.yaml": MINIMAL + "key: ex99/somewhere_else\n"}))

    assert task.spec.key == "p1"


def test_a_statement_file_replaces_the_one_in_the_yaml() -> None:
    bundle = zipped(
        {
            "p1/task.yaml": "statement: 埋め込みの本文\n",
            "p1/statement.md": "## [必須] 別の本文 ##\n\nこちらが勝つ",
        }
    )

    (task,) = read_bundle(bundle)

    assert "こちらが勝つ" in task.spec.statement


def test_test_cases_are_read_in_pairs() -> None:
    bundle = zipped(
        {
            "p1/task.yaml": MINIMAL,
            "p1/tests/case1.in": "3 1 2\n",
            "p1/tests/case1.out": "3\n",
            "p1/tests/case2.in": "1\n",
            "p1/tests/case2.out": "1\n",
        }
    )

    (task,) = read_bundle(bundle)

    assert [case.name for case in task.spec.test_cases] == ["case1", "case2"]
    assert task.spec.test_cases[0].input == "3 1 2\n"
    assert task.spec.test_cases[0].expected == "3\n"
    assert task.spec.auto_graded


def test_a_test_case_without_its_expected_output_is_refused() -> None:
    """期待出力が無いと、通ったのか確かめていないのか区別できない。"""
    with pytest.raises(AdminError, match=r"case1\.out がありません"):
        read_bundle(zipped({"p1/task.yaml": MINIMAL, "p1/tests/case1.in": "x"}))


def test_an_expected_output_without_its_input_is_refused() -> None:
    with pytest.raises(AdminError, match="入力の無い期待出力"):
        read_bundle(zipped({"p1/task.yaml": MINIMAL, "p1/tests/case1.out": "x"}))


def test_a_reference_solution_is_read() -> None:
    bundle = zipped({"p1/task.yaml": MINIMAL, "p1/reference.c": "int main(void){return 0;}"})

    (task,) = read_bundle(bundle)

    assert task.spec.reference_solution == "int main(void){return 0;}"


def test_images_are_carried_but_not_stored() -> None:
    """読むだけ。保存先を決めるのは呼び出し側（既存の画像ストア）。"""
    bundle = zipped({"p1/task.yaml": MINIMAL, "p1/images/fig1.png": b"\x89PNG\r\n"})

    (task,) = read_bundle(bundle)

    assert [image.name for image in task.images] == ["fig1.png"]
    assert task.images[0].payload.startswith(b"\x89PNG")


def test_several_tasks_come_back_in_order() -> None:
    bundle = zipped({"p2/task.yaml": MINIMAL, "p1/task.yaml": MINIMAL})

    tasks = read_bundle(bundle)

    assert [task.leaf for task in tasks] == ["p1", "p2"]


def test_a_wrapping_directory_is_allowed() -> None:
    """`ex06.zip` の中身が `ex06/p1` でも `p1` でも読める。"""
    bundle = zipped({"ex06/p1/task.yaml": MINIMAL})

    (task,) = read_bundle(bundle)

    assert task.leaf == "p1"


def test_macos_metadata_is_ignored() -> None:
    bundle = zipped({"p1/task.yaml": MINIMAL, "__MACOSX/._p1": b"junk"})

    assert len(read_bundle(bundle)) == 1


# --------------------------------------------------------------------------
# 間違いは、そのまま読める言葉で断る
# --------------------------------------------------------------------------


def test_a_misspelled_field_is_refused_with_the_field_name() -> None:
    """`extra="forbid"` が綴り間違いを読み込み時に落とす。"""
    with pytest.raises(AdminError, match="statment"):
        read_bundle(zipped({"p1/task.yaml": "statement: 本文\nstatment: 打ち間違い\n"}))


def test_broken_yaml_is_refused() -> None:
    with pytest.raises(AdminError, match="YAML として読めません"):
        read_bundle(zipped({"p1/task.yaml": "statement: [unclosed\n"}))


def test_a_bundle_without_any_task_yaml_is_refused() -> None:
    with pytest.raises(AdminError, match="課題が 1 件も見つかりません"):
        read_bundle(zipped({"readme.txt": "何もない"}))


def test_a_task_yaml_at_the_root_is_refused() -> None:
    with pytest.raises(AdminError, match="直下"):
        read_bundle(zipped({"task.yaml": MINIMAL}))


def test_duplicate_leaf_names_are_refused() -> None:
    """同じ鍵になる 2 件を黙って片方だけ入れない。"""
    with pytest.raises(AdminError, match="同じ名前"):
        read_bundle(zipped({"a/p1/task.yaml": MINIMAL, "b/p1/task.yaml": MINIMAL}))


def test_a_file_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(AdminError, match="UTF-8"):
        read_bundle(zipped({"p1/task.yaml": b"statement: \xff\xfe"}))
