"""隔離実行の語彙（S4）。

提出コードを動かす経路はすべてここを通す。評価器が subprocess を
直接叩けないようにするのが目的（ADR 0006）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Isolation(StrEnum):
    """どの水準で隔離されているか。

    採点結果に記録する。「サンドボックスで動かした」と言えるかどうかは
    後から監査できなければ意味がない。
    """

    NONE = "none"
    """隔離なし。開発時に自分の書いたコードを通すためだけのもの。"""

    OS_SANDBOX = "os_sandbox"
    """OS のサンドボックス機構（macOS seatbelt 等）。
    ネットワークと書き込みは止まるが、カーネルは共有している。"""

    CONTAINER = "container"
    """コンテナ。名前空間・cgroup で分離。"""

    KERNEL_ISOLATED = "kernel_isolated"
    """gVisor / VM。カーネルインターフェースまで分離。"""


class Limitation(StrEnum):
    """このバックエンドが**守れないもの**の自己申告。

    隔離は環境ごとに穴の位置が違う。「サンドボックスに入れた」で
    一括りにすると、穴の上に乗ったまま実提出を通すことになる。
    バックエンド自身に申告させ、危険な試験や実運用の可否を
    この申告で判断する（ADR 0006）。
    """

    NO_ISOLATION = "no_isolation"
    """隔離が無い。提出物には使えない。"""

    SHARED_KERNEL = "shared_kernel"
    """ホストとカーネルを共有する。カーネルの脆弱性で抜けられる。"""

    SHARED_UID = "shared_uid"
    """ホストの利用者と同じ UID で動く。UID 単位の資源はホストと共有。"""

    PROCESS_LIMIT_UNENFORCED = "process_limit_unenforced"
    """プロセス数を強制できない。**fork bomb を封じ込められない。**

    実測（macOS seatbelt, 2026-08）: RLIMIT_NPROC は UID 単位の上限なので、
    暴走したプロセス群がホスト利用者のプロセス表を埋め、シェルが
    fork できなくなった。上限は掛かるが、被害はサンドボックスの外に出る。
    """


class Limits(BaseModel):
    """資源上限。

    壁時計だけでは足りない。CPU を回し続ける提出は CPU 上限の方が早く止まり、
    メモリとプロセス数の上限が無いとホストごと巻き込まれる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_seconds: int = Field(default=5, ge=1)
    wall_seconds: float = Field(default=10.0, gt=0.0)
    memory_bytes: int = Field(default=512 * 1024 * 1024, ge=16 * 1024 * 1024)
    processes: int = Field(default=64, ge=1)
    output_bytes: int = Field(default=1024 * 1024, ge=1024)


class ExecRequest(BaseModel):
    """作業域の中で 1 コマンド動かす要求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(min_length=1)
    stdin: str = ""
    limits: Limits = Limits()
    env: dict[str, str] = Field(default_factory=dict)
    # ネットワークは既定で遮断。開ける用途は今のところ無いが、
    # 「既定で閉じている」ことを明示するためにフラグにしてある。
    network: bool = False
    # コンパイラのように、信頼できる実行体が OS の一時領域を要る場合だけ真。
    # 提出物そのものを動かすときは常に偽。
    trusted_toolchain: bool = False


class ExecResult(BaseModel):
    """実行結果。失敗も例外ではなく結果として返す。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    signal_name: str | None = None
    truncated: bool = False
    isolation: Isolation = Isolation.NONE

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def killed(self) -> bool:
        """時間切れか、シグナルで殺されたか。"""
        return self.timed_out or self.signal_name is not None


class SandboxError(Exception):
    """サンドボックスそのものが使えない。実行結果の失敗とは別。"""


class SandboxUnavailable(SandboxError):
    """要求された隔離手段がこの環境に無い。

    黙って隔離なしに落とさない。落とすくらいなら採点を失敗させる。
    """


class UnsafeSandboxRefused(SandboxError):
    """隔離なしの実行を、明示的な許可なしに要求された。"""
