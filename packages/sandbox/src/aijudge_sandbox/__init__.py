"""aiJudge sandbox (S4) — 提出コードを隔離して実行する。

提出物を動かす経路はここだけにする。評価器が subprocess を直接叩けると、
どこか一箇所で隔離が抜ける。抜けたことに気づく方法も無い。

規則:

- 隔離手段が無ければ**採点を失敗させる**。隔離なしに自動で落ちない。
- 隔離なしのバックエンドは、明示的な許可を 2 つ要求する。
- 実際に使った隔離の水準（`Isolation`）を結果に載せ、後から監査できるようにする。
"""

from __future__ import annotations

from .backends import (
    DEFAULT_IMAGE,
    DockerSandbox,
    SeatbeltSandbox,
    UnsafeLocalSandbox,
)
from .base import Sandbox, Workspace
from .selection import (
    BACKENDS,
    ENV_BACKEND,
    ENV_IMAGE,
    build_sandbox,
    default_sandbox,
)
from .types import (
    ExecRequest,
    ExecResult,
    Isolation,
    Limitation,
    Limits,
    SandboxError,
    SandboxUnavailable,
    UnsafeSandboxRefused,
)

__all__ = [
    "BACKENDS",
    "DEFAULT_IMAGE",
    "ENV_BACKEND",
    "ENV_IMAGE",
    "DockerSandbox",
    "ExecRequest",
    "ExecResult",
    "Isolation",
    "Limitation",
    "Limits",
    "Sandbox",
    "SandboxError",
    "SandboxUnavailable",
    "SeatbeltSandbox",
    "UnsafeLocalSandbox",
    "UnsafeSandboxRefused",
    "Workspace",
    "build_sandbox",
    "default_sandbox",
]
