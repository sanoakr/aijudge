"""保存層の UnitOfWork が、13 のリポジトリの Protocol を満たす（段階 2-2）。

`isinstance` は Protocol の**属性があるか**しか見ない。`reviews` が
`ReviewStore` かどうかまでは確かめないので、Protocol の注釈を読んで、
各リポジトリがその型の Protocol を満たすかを 1 つずつ確かめる。
リポジトリを Protocol に足せば、このテストも自動で広がる。
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from aijudge_persistence import Database
from aijudge_unit_of_work import StoreUnitOfWork, UnitOfWork

# 13 のリポジトリ。**数を固定する** ── 保存層に足して Protocol に足し忘れると、
# ここが落ちる（下の突き合わせは Protocol に書いたものしか見ない）。
REPOSITORY_COUNT = 13


def _repositories(protocol: type) -> dict[str, type]:
    """Protocol がプロパティで宣言したリポジトリの名前と型。"""
    found: dict[str, type] = {}
    for name, member in inspect.getmembers(protocol):
        if isinstance(member, property) and member.fget is not None:
            found[name] = get_type_hints(member.fget)["return"]
    return found


@pytest.fixture
def uow():
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as opened:
            yield opened
    finally:
        database.dispose()


def test_the_protocol_names_every_repository_the_store_opens(uow) -> None:
    declared = set(_repositories(UnitOfWork))
    assert len(declared) == REPOSITORY_COUNT
    opened = {
        name
        for name, value in vars(uow).items()
        if not name.startswith("_") and not callable(value)
    }
    assert opened == declared, f"opened but not declared: {sorted(opened - declared)}"


@pytest.mark.parametrize("protocol", [UnitOfWork, StoreUnitOfWork])
def test_the_sql_unit_of_work_keeps_the_protocol(uow, protocol) -> None:
    assert isinstance(uow, protocol)
    for name, repository_type in _repositories(protocol).items():
        assert isinstance(getattr(uow, name), repository_type), (
            f"uow.{name} does not satisfy {repository_type.__name__}"
        )


def test_the_store_narrows_only_the_queries_that_need_the_tables() -> None:
    """保存層だけが満たすのはレビューと課題の 2 つ。他を狭めていない。"""
    wide = _repositories(UnitOfWork)
    narrow = _repositories(StoreUnitOfWork)
    changed = {name for name in wide if narrow[name] is not wide[name]}
    assert changed == {"reviews", "tasks"}
