"""`AdminError` は `aijudge_course_admin.errors` に移った（段階 3-1）。

移す間は旧い名前（`aijudge_admin.AdminError`・`aijudge_admin.operations.AdminError`）
でも同じクラスを指す。**別のクラスになると、旧い名前で `except` している
コンソールが新しい場所から投げられた例外を取り逃がし、400 の代わりに 500 を返す。**
"""

from __future__ import annotations

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
