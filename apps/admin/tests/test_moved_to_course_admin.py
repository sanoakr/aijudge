"""`apps/admin` から `aijudge_course_admin` へ移したものが、旧い名前でも同じものを指す。

`AdminError` は `aijudge_course_admin.errors` に移った（段階 3-1）。

移す間は旧い名前（`aijudge_admin.AdminError`・`aijudge_admin.operations.AdminError`）
でも同じクラスを指す。**別のクラスになると、旧い名前で `except` している
コンソールが新しい場所から投げられた例外を取り逃がし、400 の代わりに 500 を返す。**
"""

from __future__ import annotations

import importlib

import pytest

import aijudge_admin
import aijudge_admin.operations
from aijudge_admin import tasks
from aijudge_course_admin import AdminError
from aijudge_course_admin.errors import AdminError as FromErrors


def test_the_old_names_are_the_same_class() -> None:
    assert aijudge_admin.AdminError is AdminError
    assert aijudge_admin.operations.AdminError is AdminError
    assert FromErrors is AdminError


def test_a_moved_module_raises_what_the_old_name_catches() -> None:
    """張り替えたモジュールが投げたものを、旧い名前で捕まえられる。"""
    assert tasks.AdminError is AdminError
    with pytest.raises(aijudge_admin.AdminError):
        raise tasks.AdminError("x")


MOVED_MODULES = ("roster", "answer_mode", "justification", "drafting", "revision")


@pytest.mark.parametrize("name", MOVED_MODULES)
def test_a_moved_module_is_the_same_module_under_the_old_name(name: str) -> None:
    """段階 3-2。**旧い名前は同じモジュールを指す**（写しではない）ので、テストが
    旧い名前で属性を差し替えても新しい場所に効く。
    """
    old = importlib.import_module(f"aijudge_admin.{name}")
    new = importlib.import_module(f"aijudge_course_admin.{name}")
    assert old is new
