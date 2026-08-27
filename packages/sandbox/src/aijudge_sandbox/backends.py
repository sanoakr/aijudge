"""バックエンド実装。

隔離の強さは環境によって違う。弱い方に黙って落ちないことが要点で、
選択は `selection.py` が明示的に行う。
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from .base import LocalSandboxBase, Workspace
from .types import (
    ExecRequest,
    Isolation,
    Limitation,
    SandboxUnavailable,
    UnsafeSandboxRefused,
)

_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")


def _base_env(request: ExecRequest, workdir: Path) -> dict[str, str]:
    """親の環境をそのまま渡さない。渡すと資格情報が提出コードに見える。"""
    env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["HOME"] = str(workdir)
    env["TMPDIR"] = str(workdir)
    env.update(request.env)
    return env


# --------------------------------------------------------------------------
# 隔離なし
# --------------------------------------------------------------------------


class UnsafeLocalSandbox(LocalSandboxBase):
    """隔離せずにホストで動かす。

    **提出物には使えない。** 明示的に `allow_unsafe=True` を渡さない限り
    作業域を開く時点で例外にする。「うっかり本番で使う」経路を塞ぐため、
    警告コメントではなく実行時の拒否にしてある。
    """

    name = "unsafe"
    isolation = Isolation.NONE
    limitations = frozenset(Limitation)

    def __init__(self, *, allow_unsafe: bool = False) -> None:
        self._allowed = allow_unsafe

    @contextlib.contextmanager
    def workspace(self) -> Iterator[Workspace]:
        if not self._allowed:
            raise UnsafeSandboxRefused(
                "refusing to execute without isolation; "
                "pass allow_unsafe=True only for code you wrote yourself"
            )
        with super().workspace() as ws:
            yield ws

    def wrap(
        self, argv: list[str], request: ExecRequest, workdir: Path
    ) -> tuple[list[str], dict[str, str]]:
        return argv, _base_env(request, workdir)


# --------------------------------------------------------------------------
# macOS seatbelt
# --------------------------------------------------------------------------

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# 読み取りは広く許し、書き込みと通信を止める方針。
# 提出コードが /etc を読めても、送信も改変もできなければ被害は限定される。
# 逆に読み取りを絞りすぎるとツールチェインが動かない。
# 家目録だけは明示的に拒否する（鍵・トークン・他学生の答案がある）。
_PROFILE = """(version 1)
(deny default)
(allow process-exec*)
(allow process-fork)
(allow sysctl-read)
(allow mach*)
(allow ipc-posix-shm*)
(allow signal (target self))
(allow file-read*)
(deny file-read* (subpath "{home}"))
(allow file-read* (subpath "{workdir}"))
(allow file-write* (subpath "{workdir}") (literal "/dev/null"))
(allow file-ioctl)
{extra}
"""


def _docker_runtimes(binary: str) -> frozenset[str]:
    """デーモンに届くか確かめ、使えるランタイムを返す。"""
    try:
        completed = subprocess.run(
            [binary, "info", "--format", "{{range $name, $_ := .Runtimes}}{{$name}} {{end}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailable(f"cannot reach the container daemon: {exc}") from exc
    if completed.returncode != 0:
        raise SandboxUnavailable(
            f"cannot reach the container daemon: {completed.stderr.strip()[:200]}"
        )
    return frozenset(completed.stdout.split())


def _resolved_darwin_temp() -> str | None:
    try:
        raw = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return str(Path(raw).resolve()) if raw else None


class SeatbeltSandbox(LocalSandboxBase):
    """macOS の `sandbox-exec`（Seatbelt）で包む。

    確認済みの効果:
      - ネットワーク到達を拒否
      - 作業域の外への書き込みを拒否
      - 利用者の家目録の読み取りを拒否

    コンテナではない。カーネルは共有しているし、カーネルの脆弱性を
    突かれれば抜けられる。Apple はこのコマンドを deprecated としている。
    Linux ホストに移すまでの手段であり、`Isolation.OS_SANDBOX` と
    記録して、コンテナと同じ強度だと誤認しないようにしている。
    """

    name = "seatbelt"
    isolation = Isolation.OS_SANDBOX
    # カーネルも UID もホストと共有する。特に fork bomb は封じ込められない
    # （実測で採点機のプロセス表を埋めた）。実提出は Linux + コンテナで動かす。
    limitations = frozenset(
        {
            Limitation.SHARED_KERNEL,
            Limitation.SHARED_UID,
            Limitation.PROCESS_LIMIT_UNENFORCED,
        }
    )

    def __init__(self) -> None:
        if platform.system() != "Darwin":
            raise SandboxUnavailable("seatbelt is only available on macOS")
        if not Path(SANDBOX_EXEC).is_file():
            raise SandboxUnavailable(f"{SANDBOX_EXEC} is missing")
        self._temp = _resolved_darwin_temp()

    def wrap(
        self, argv: list[str], request: ExecRequest, workdir: Path
    ) -> tuple[list[str], dict[str, str]]:
        extra = ""
        if request.trusted_toolchain and self._temp:
            # clang は OS のユーザー一時領域に中間ファイルを書く。
            # 提出物そのものを動かすときは許さない。
            extra = f'(allow file-write* (subpath "{self._temp}"))'

        profile = _PROFILE.format(
            home=str(Path.home().resolve()),
            workdir=str(workdir.resolve()),
            extra=extra,
        )
        profile_path = workdir / ".aijudge-profile.sb"
        profile_path.write_text(profile, encoding="utf-8")

        return [SANDBOX_EXEC, "-f", str(profile_path), *argv], _base_env(request, workdir)


# --------------------------------------------------------------------------
# コンテナ
# --------------------------------------------------------------------------

DEFAULT_IMAGE = "gcc:14-bookworm"


class DockerSandbox(LocalSandboxBase):
    """コンテナで包む。`runtime` に `runsc` を渡せば gVisor になる。

    作業域をホストから読み書き可能に bind mount する。
    ルートは read-only、ネットワークは無し、権限は全部落とす。
    """

    name = "docker"

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        binary: str = "docker",
        runtime: str | None = None,
    ) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise SandboxUnavailable(f"{binary} is not installed")
        self._binary = resolved
        # CLI があることと動くことは別。デーモンが落ちている、runsc が
        # 入っていない、といった状態で「隔離できている」と誤認しないよう、
        # ここで確かめる。自動選択が弱い方へ落ちる判断もこの結果で決まる。
        runtimes = _docker_runtimes(resolved)
        if runtime is not None and runtime not in runtimes:
            raise SandboxUnavailable(
                f"{binary} has no {runtime!r} runtime (available: {sorted(runtimes)})"
            )
        self._image = image
        self._runtime = runtime
        self.isolation = Isolation.KERNEL_ISOLATED if runtime == "runsc" else Isolation.CONTAINER
        self.name = f"docker:{runtime}" if runtime else "docker"
        # --pids-limit と --user で、プロセス数も UID もホストから切り離せる。
        # 残る穴はカーネル共有だけで、それは gVisor（runsc）で塞がる。
        self.limitations = (
            frozenset() if runtime == "runsc" else frozenset({Limitation.SHARED_KERNEL})
        )

    def wrap(
        self, argv: list[str], request: ExecRequest, workdir: Path
    ) -> tuple[list[str], dict[str, str]]:
        limits = request.limits
        command = [
            self._binary,
            "run",
            "--rm",
            "--interactive",
            "--network=none" if not request.network else "--network=bridge",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            # root で動かさない。作業域は誰でも書ける前提で用意する。
            "--user=65534:65534",
            f"--pids-limit={limits.processes}",
            f"--memory={limits.memory_bytes}",
            # swap を許すとメモリ上限が意味を失う。
            f"--memory-swap={limits.memory_bytes}",
            f"--ulimit=cpu={limits.cpu_seconds}",
            f"--ulimit=fsize={limits.output_bytes}",
            "--workdir=/work",
            f"--volume={workdir}:/work",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        ]
        if self._runtime:
            command.append(f"--runtime={self._runtime}")
        for key, value in request.env.items():
            command.append(f"--env={key}={value}")
        command.append(self._image)
        command.extend(argv)

        # docker クライアント自身の環境。中に渡るのは --env で明示した分だけ。
        return command, {
            key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST") if key in os.environ
        }
