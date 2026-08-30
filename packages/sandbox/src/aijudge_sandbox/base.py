"""作業域とバックエンド共通の実行処理。

バックエンドの違いは「argv をどう包むか」だけに閉じる。
プロセス起動・上限適用・出力の切り詰めは全バックエンドで同じ。
"""

from __future__ import annotations

import contextlib
import functools
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from .types import ExecRequest, ExecResult, Isolation, Limitation, Limits


def _apply_limit(which: int, soft: int) -> None:
    """rlimit を 1 つ、ハード上限を超えない範囲で設定する。

    環境によっては設定できない（macOS の RLIMIT_AS 等）。preexec_fn の中で
    例外を投げるとプロセスごと起動できなくなるため、設定できないものは諦める。
    これは隔離の代わりではなく、隔離に足す事故防止。
    """
    try:
        _, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            soft = min(soft, hard)
        resource.setrlimit(which, (soft, hard))
    except (ValueError, OSError):
        pass


@functools.cache
def _user_process_count() -> int:
    """このユーザーが今動かしているプロセス数。

    RLIMIT_NPROC の基準に要る。1 回だけ数えて使い回す。
    """
    try:
        completed = subprocess.run(
            ["/bin/ps", "-u", str(os.getuid()), "-o", "pid="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 512
    return max(1, len(completed.stdout.splitlines()))


def make_limiter(limits: Limits) -> Callable[[], None]:
    """子プロセスに rlimit を掛ける。

    RLIMIT_NPROC の扱いに注意が要る。これは「このプロセスの子」ではなく
    **このユーザーの全プロセス**の上限なので、絶対値で小さく設定すると
    コンパイラすら起動できない（実測: posix_spawn failed: Resource
    temporarily unavailable）。現在の使用数に上乗せする形で掛ける。

    これで fork bomb は上限で頭打ちになるが、**完全な封じ込めではない**。
    暴走中は同じユーザーの他のプロセスも起動しにくくなる。
    本番でプロセス数を確実に区切るには、専用 UID かコンテナの
    `--pids-limit` が要る（ADR 0006）。
    """
    ceiling = _user_process_count() + limits.processes

    def limit() -> None:
        _apply_limit(resource.RLIMIT_CPU, limits.cpu_seconds)
        _apply_limit(resource.RLIMIT_AS, limits.memory_bytes)
        _apply_limit(resource.RLIMIT_NPROC, ceiling)
        _apply_limit(resource.RLIMIT_FSIZE, limits.output_bytes)

    return limit


def _kill_group(process: subprocess.Popen[str]) -> None:
    """子プロセスグループを丸ごと落とす。

    子が孫を撒いている場合、親だけ殺しても孫は生き残ってホストの
    CPU を食い続ける。setsid してあるので、グループ ID = 子の PID。
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()


def _signal_of_negative_code(code: int) -> str | None:
    """直接の子プロセスがシグナルで死んだ場合の解釈。

    `subprocess` は子が signal N で死ぬと `-N` を返す。**間に別のプロセスが
    入る構成ではこれが効かない**（コンテナバックエンドは自分の規約で
    読み替える。`LocalSandboxBase.decode_signal` 参照）。
    """
    if code >= 0:
        return None
    try:
        return signal.Signals(-code).name
    except ValueError:
        return None


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap], True


@runtime_checkable
class Workspace(Protocol):
    """1 件の採点のあいだだけ存在する作業域。

    コンパイルと実行を跨いでファイルが残る必要があるので、
    実行 1 回ごとではなく作業域を単位にしてある。
    """

    path: Path

    def write(self, name: str, content: bytes | str) -> Path: ...

    def run(self, request: ExecRequest) -> ExecResult: ...


@runtime_checkable
class Sandbox(Protocol):
    name: str
    isolation: Isolation

    limitations: frozenset[Limitation]
    """このバックエンドが守れないもの。空集合は「穴の申告なし」。

    呼ぶ側はこれを見て判断する。`isolation` の強弱だけでは、
    どの攻撃が通るかが分からない。
    """

    def workspace(self) -> contextlib.AbstractContextManager[Workspace]: ...


class LocalWorkspace:
    """ホスト上の一時ディレクトリを作業域にする作業域。

    バックエンドは `wrap` で argv を包むだけでよい。
    """

    def __init__(
        self,
        path: Path,
        isolation: Isolation,
        wrap: Callable[[list[str], ExecRequest, Path], tuple[list[str], dict[str, str]]],
        decode_signal: Callable[[int], str | None] | None = None,
        *,
        apply_host_rlimits: bool = True,
    ) -> None:
        self.path = path
        self._isolation = isolation
        self._wrap = wrap
        self._decode_signal = decode_signal or _signal_of_negative_code
        # docker では起動するのはコンテナではなく docker クライアント自身
        # （Go 製バイナリ）。RLIMIT_AS を提出物のつもりでここへ掛けると、
        # Go ランタイムの起動時メモリ予約が失敗してクライアントごと落ちる
        # （実測: page summary memory の確保失敗）。コンテナ側の制限は
        # 別途 --memory / --pids-limit / --ulimit で掛かっているので、
        # このバックエンドではホスト側の rlimit を掛けない。
        self._apply_host_rlimits = apply_host_rlimits

    def write(self, name: str, content: bytes | str) -> Path:
        target = self.path / name
        if not target.resolve().is_relative_to(self.path.resolve()):
            raise ValueError(f"{name!r} escapes the workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_bytes(content)
        return target

    def run(self, request: ExecRequest) -> ExecResult:
        argv, env = self._wrap(list(request.argv), request, self.path)
        started = time.monotonic()

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.path,
                env=env,
                # 独立したセッションにして、孫まで一括で落とせるようにする。
                start_new_session=True,
                preexec_fn=make_limiter(request.limits) if self._apply_host_rlimits else None,
            )
        except OSError as exc:
            return ExecResult(
                exit_code=-1,
                stderr=f"failed to start: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                isolation=self._isolation,
            )

        timed_out = False
        try:
            raw_out, raw_err = process.communicate(
                input=request.stdin, timeout=request.limits.wall_seconds
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            with contextlib.suppress(subprocess.TimeoutExpired, ValueError):
                raw_out, raw_err = process.communicate(timeout=5)
            raw_out = raw_out or ""
            raw_err = raw_err or ""
        finally:
            # 正常終了でも撒かれた孫が残ることがある。必ず掃除する。
            _kill_group(process)

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, cut_out = _truncate(raw_out or "", request.limits.output_bytes)
        stderr, cut_err = _truncate(raw_err or "", request.limits.output_bytes)

        code = process.returncode if process.returncode is not None else -1
        signal_name = self._decode_signal(code)
        if signal_name is not None:
            # CPU 上限やメモリ上限で殺されたのは時間切れと同じ意味。
            timed_out = timed_out or signal_name in ("SIGXCPU", "SIGKILL")

        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            signal_name=signal_name,
            truncated=cut_out or cut_err,
            isolation=self._isolation,
        )


class LocalSandboxBase:
    """ホスト上に作業域を作るバックエンドの共通部分。

    ホストのプロセス表と UID を共有するので、既定でその 2 つを申告する。

    作業域の置き場所を差し替えられるようにしてあるのは、コンテナ
    バックエンドのため。**ホストの一時ディレクトリはコンテナから見えない
    ことがある**（macOS の `/var/folders/...` は colima / Docker Desktop の
    既定のマウント対象に入らない）。見えないまま bind mount すると
    **空のディレクトリがマウントされ、提出物が存在しないまま採点が走る**。
    """

    name = "local"
    isolation = Isolation.NONE
    limitations = frozenset(
        {
            Limitation.SHARED_KERNEL,
            Limitation.SHARED_UID,
            Limitation.PROCESS_LIMIT_UNENFORCED,
        }
    )
    workspace_root: Path | None = None
    # 起動する子プロセスが提出物そのもの（またはそこへ exec する薄いラッパー）
    # であるバックエンドは True のまま。子プロセスが提出物を包む別のランタイム
    # （docker クライアントなど）であるバックエンドは False にして上書きする。
    apply_host_rlimits: bool = True
    # 作業域を、ホストの作成者とは別の uid から読み書きできるようにするか。
    # 同じホスト uid で提出物を動かすバックエンド（unsafe / seatbelt）は
    # mkdtemp の既定（作成者のみ）で足りる。コンテナの中で固定の別 uid
    # （nobody 等）から書く docker は、これが要る側で上書きする。
    world_writable_workspace: bool = False

    def decode_signal(self, code: int) -> str | None:
        """終了コードからシグナル名を引く。

        既定は「直接の子が死んだ場合」だけを見る。プロセスの間に別の
        プロセスが挟まるバックエンドは、自分の規約で上書きする。
        """
        return _signal_of_negative_code(code)

    @contextlib.contextmanager
    def workspace(self) -> Iterator[Workspace]:
        root = self.workspace_root
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        directory = Path(
            tempfile.mkdtemp(prefix="aijudge-ws-", dir=None if root is None else str(root))
        ).resolve()
        if self.world_writable_workspace:
            # コンテナの中は固定の別 uid（docker では nobody）で動く。
            # 「作業域は誰でも書ける前提」（backends.py の DockerSandbox.wrap）を
            # 実際に成立させるのはここ。世代限りの一時ディレクトリなので、
            # 世界書き込み可でも外に漏れる情報はない。
            directory.chmod(0o777)
        try:
            yield LocalWorkspace(
                directory,
                self.isolation,
                self.wrap,
                self.decode_signal,
                apply_host_rlimits=self.apply_host_rlimits,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def wrap(
        self, argv: list[str], request: ExecRequest, workdir: Path
    ) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError
