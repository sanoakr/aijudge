"""課題文の改行は `\\n` に揃える。

課題文は 3 つの経路から来る ── `desc.md`（Unix 改行）、教員コンソールの
`<textarea>`（ブラウザは慣習的に `\\r\\n` を送る）、AI 作問。改行コードが
経路ごとに違うと、同じ文面のはずの 2 つの `TaskVersion` が別物に見える。

実測（2026-09-22）: コンソールから保存した課題文が DB に `\\r\\n` のまま
入っており、`desc.md`（`\\n`）と比べる側（`course export` / `course diff`）が
「内容が違う」と報告した。`course export` の自己検算（書いたものを読み直して
確かめる）も、`Path.write_text` が `\\r\\n` をそのまま書く一方で
`Path.read_text` は universal newlines で `\\n` に潰すため、そこで素通りして
いた `\\r` に足を取られて落ちていた。

`TaskSpec` が課題を足す唯一の入口である以上、正規化もここ 1 か所で行う。
"""

from __future__ import annotations

from aijudge_authoring import TaskSpec


def test_crlf_becomes_lf() -> None:
    spec = TaskSpec(key="ex1/p1", statement="## 課題 ##\r\n\r\n本文\r\n")
    assert "\r" not in spec.statement
    assert spec.statement == "## 課題 ##\n\n本文\n"


def test_a_lone_cr_becomes_lf_too() -> None:
    """`\\r` 単独（古い Mac 由来）も同じ扱いにする。中途半端に残すと意味が無い。"""
    spec = TaskSpec(key="ex1/p1", statement="## 課題 ##\r\r本文")
    assert "\r" not in spec.statement


def test_plain_lf_is_left_alone() -> None:
    """既に `\\n` のものは触らない ── 変わらないことも固定しておく。"""
    spec = TaskSpec(key="ex1/p1", statement="## 課題 ##\n\n本文\n")
    assert spec.statement == "## 課題 ##\n\n本文\n"


def test_two_statements_that_only_differ_by_line_ending_declare_the_same_content() -> None:
    """これが正規化の目的 ── `desc.md` と教員コンソールの保存が同じ扱いになる。"""
    from_file = TaskSpec(key="ex1/p1", statement="## 課題 ##\n\n本文\n")
    from_console = TaskSpec(key="ex1/p1", statement="## 課題 ##\r\n\r\n本文\r\n")
    assert from_file.statement == from_console.statement
