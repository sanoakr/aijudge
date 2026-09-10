"""ログに載せる文脈（相関 ID と識別子）。

**ここに載せてよいのは識別子だけ。** 提出物の本文・氏名・メール・LLM の
プロンプトと出力は載せない（設計原則 P7）。運用ログは journald に出て
ローテートで消える場所であり、バックアップも暗号化も掛かっていない。
学習者データをそこへ複製した時点で、ローカルモデルにしか流さないという
ゲートウェイ側の保証（ADR 0004）が迂回される。

規約だけでは守れないので、`bind` は値の型と長さを見て弾く。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

# 識別子の長さの上限。UUID（36 文字）とホスト名が入れば足りる。
# これを超える値は、識別子ではなく中身を載せようとしている。
MAX_VALUE_LENGTH = 128

# 既定を `None` にしてあるのは、可変オブジェクトを ContextVar の既定に置くと
# 全コンテキストで同じ辞書を共有してしまうため（ruff B039）。
_context: ContextVar[Mapping[str, str | int | float | bool] | None] = ContextVar(
    "aijudge_log_context", default=None
)


class ContextValueRejected(ValueError):
    """文脈に載せられない値。識別子ではないものを載せようとしている。"""


def _check(key: str, value: object) -> str | int | float | bool:
    if isinstance(value, bool | int | float):
        return value
    if not isinstance(value, str):
        raise ContextValueRejected(
            f"{key!r}: ログの文脈に載せてよいのは識別子（文字列・数値・真偽値）だけです"
            f"（受け取ったのは {type(value).__name__}）"
        )
    if len(value) > MAX_VALUE_LENGTH:
        raise ContextValueRejected(
            f"{key!r}: {len(value)} 文字は識別子として長すぎます"
            f"（上限 {MAX_VALUE_LENGTH}）。本文をログに載せていないか確認してください"
        )
    return value


def current_context() -> Mapping[str, str | int | float | bool]:
    """いま有効な文脈。フォーマッタが読む。"""
    return _context.get() or {}


@contextmanager
def bind(**fields: object) -> Iterator[None]:
    """この区間のログに識別子を足す。

    入れ子にすると内側が外側を上書きせずに重ねる。抜けると元に戻るので、
    ワーカーのループで 1 件ごとに `bind(job_id=...)` して構わない。
    """
    checked = {key: _check(key, value) for key, value in fields.items()}
    token = _context.set({**current_context(), **checked})
    try:
        yield
    finally:
        _context.reset(token)
