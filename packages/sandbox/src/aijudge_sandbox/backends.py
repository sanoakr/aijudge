"""バックエンド実装。

隔離の強さは環境によって違う。弱い方に黙って落ちないことが要点で、
選択は `selection.py` が明示的に行う。
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import signal
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

from .base import LocalSandboxBase, Workspace
from .types import (
    ExecRequest,
    ExecResult,
    Isolation,
    Limitation,
    Limits,
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

# コンテナバックエンドの作業域の既定の置き場所。
#
# ホストの一時ディレクトリ（macOS の `/var/folders/...`）を使わないのは、
# colima / Docker Desktop がそこを VM にマウントしないため。マウントされない
# パスを bind mount すると**エラーにならず空のディレクトリが見える**ので、
# 提出物が存在しないまま採点が走り、全員がコンパイルエラーで 0 点になる。
# 家目録の下は両者が既定でマウントする。
DEFAULT_WORKSPACE_ROOT = Path.home() / ".aijudge" / "work"
ENV_WORKSPACE_ROOT = "AIJUDGE_SANDBOX_WORKDIR"

logger = logging.getLogger(__name__)

# 提出物を動かすコンテナに付ける印（#410）。時間切れの後始末は名前で、
# 取りこぼしの棚卸しはこのラベルで引く:
#   docker ps --filter label=aijudge.sandbox
CONTAINER_LABEL = "aijudge.sandbox"
CONTAINER_NAME_PREFIX = "aijudge-sbx-"
# コンテナ 1 つが使える CPU の数。**CPU 時間の上限（`--ulimit=cpu`）とは別物**
# で、こちらは同時に何コア食えるか。無いと、スレッドを撒く提出 1 件が
# ホストの全コアを取り、同時に走る他の学習者の実行を遅らせる。
CONTAINER_CPUS = 1.0
# 後始末（`docker rm -f`）を待つ上限。デーモンが詰まっていても採点を止めない。
RELEASE_TIMEOUT_SECONDS = 30.0

# **実時間の上限はコンテナの中で掛ける**（#489）。以前は `docker run` の
# クライアントに掛けていたので、コンテナの起動にかかった時間まで提出物の
# 持ち時間に数えていた。起動が遅れると正しい提出が時間切れになる ──
# CI で 4 つのテストが同時にコンテナを起動したら、同じ模範解答が 1 回目は
# 0.8、2 回目は 1.0 になった（5 ケース中 1 ケースが上限 2 秒を超えた）。
# 運用機の gVisor は起動が runc より重く、締切前や試験で提出が集中すると
# 同じことが起こりうる。
#
# coreutils の `timeout` を使う。上限で SIGTERM を送り、無視されたら
# `TIMEOUT_KILL_AFTER_SECONDS` 後に SIGKILL。前者の終了コードは 124、
# 後者は 137（SIGKILL。これは前から時間切れに分類している）。
# **イメージに `timeout` が要る**（Debian 系の公式イメージには入っている）。
# 無ければ起動時の検査（`_verify_mount`）で使えないと申告する。
#
# 引数は **`-k 1 <秒>` の短い形**で渡す（#203）。Alpine の busybox の `timeout`
# は `--kill-after=` を知らず、何も起動できない（Dolos のイメージで実測）。
# 短い形は coreutils と busybox の両方が受け付ける。busybox は時間切れを 124
# ではなく 143（SIGTERM）で返すので時間切れとは呼ばれないが、`ok` は偽になる。
IN_CONTAINER_TIMEOUT = "timeout"
TIMEOUT_EXIT_CODE = 124
TIMEOUT_KILL_AFTER_SECONDS = 1
# クライアントを待つ時間の、実時間の上限への上乗せ。コンテナの起動と後始末の
# ぶんで、ふだんは 1 秒に満たない。**持ち時間ではなく歯止め**である ──
# 提出物は中の `timeout` で止まるので、これが効くのはデーモンが詰まったとき
# だけ（そのときは #410 の後始末でコンテナを名指しで止める）。
STARTUP_ALLOWANCE_SECONDS = 15.0

# マウント検証に使う目印。中身まで一致を見るのは、
# 「ディレクトリは見えるが中身が古い」構成（キャッシュされた共有）も落とすため。
_MOUNT_PROBE = "aijudge-mount-probe"
_MOUNT_TOKEN = "mounted"


def _lacks_timeout(result: ExecResult) -> bool:
    """イメージに `timeout` が無くて、何も起動できなかったか。

    docker は起動できないコマンドを 127 で返し、理由を stderr に書く
    （`exec: "timeout": executable file not found in $PATH`）。
    """
    return result.exit_code == 127 and IN_CONTAINER_TIMEOUT in result.stderr


class DockerSandbox(LocalSandboxBase):
    """コンテナで包む。`runtime` に `runsc` を渡せば gVisor になる。

    作業域をホストから読み書き可能に bind mount する。
    ルートは read-only、ネットワークは無し、権限は全部落とす。
    """

    name = "docker"
    # 起動するのは docker クライアント（Go 製）で、提出物そのものではない。
    # RLIMIT_AS をこのプロセスに掛けると、既定の 512MB 程度でも Go ランタイムの
    # 起動時メモリ予約が失敗して落ちる（Linux で実測。macOS は RLIMIT_AS の
    # setrlimit 自体が失敗して黙って無視されるため、気づかれずにいた）。
    # 提出物側の制限はコンテナ起動フラグ（--memory / --pids-limit / --ulimit）
    # が別途課しているので、ホスト側には掛けない。
    apply_host_rlimits = False
    # コンテナの中は --user=65534:65534（nobody）で動く。ホストの作成者 uid
    # にしか開いていない作業域では読み書きできない。
    world_writable_workspace = True
    # 実時間の上限は中の `timeout` が掛ける。外はその上に起動の余裕を見る（#489）。
    startup_allowance_seconds = STARTUP_ALLOWANCE_SECONDS

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        binary: str = "docker",
        runtime: str | None = None,
        workspace_root: Path | None = None,
        verify_mount: bool = True,
        clear_entrypoint: bool = False,
    ) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise SandboxUnavailable(f"{binary} is not installed")
        self._binary = resolved
        self.workspace_root = (
            workspace_root
            or Path(os.environ.get(ENV_WORKSPACE_ROOT, DEFAULT_WORKSPACE_ROOT)).expanduser()
        )
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
        self._clear_entrypoint = clear_entrypoint
        self.isolation = Isolation.KERNEL_ISOLATED if runtime == "runsc" else Isolation.CONTAINER
        self.name = f"docker:{runtime}" if runtime else "docker"
        # --pids-limit と --user で、プロセス数も UID もホストから切り離せる。
        # 残る穴はカーネル共有だけで、それは gVisor（runsc）で塞がる。
        self.limitations = (
            frozenset() if runtime == "runsc" else frozenset({Limitation.SHARED_KERNEL})
        )
        if verify_mount:
            # 作業域がコンテナから**実際に見えるか**を一度だけ確かめる。
            # 確かめないと、マウントされていない構成で採点が「動く」。
            # 動いた結果は全員 0 点で、原因は提出物にあるように見える。
            self._verify_mount()

    def _verify_mount(self) -> None:
        """作業域が見えることを確認する。見えなければ使えないと申告する。

        「隔離できないなら採点を止める」（ADR 0006）と同じ理屈。壊れた
        サンドボックスで採点が通ってしまう方が、採点が止まるより悪い。
        """
        with super().workspace() as probe:
            probe.write(_MOUNT_PROBE, _MOUNT_TOKEN)
            result = probe.run(
                ExecRequest(
                    argv=("/bin/cat", f"/work/{_MOUNT_PROBE}"),
                    limits=Limits(cpu_seconds=10, wall_seconds=60.0),
                )
            )
            if _lacks_timeout(result):
                raise SandboxUnavailable(
                    f"the image {self._image} has no `{IN_CONTAINER_TIMEOUT}` (coreutils), "
                    f"which enforces the wall-clock limit inside the container. Without it "
                    f"no submission can run at all. Use an image that ships coreutils."
                )
            visible = result.ok and result.stdout.strip() == _MOUNT_TOKEN

            # 書き込みも確かめる。コンパイル結果を置けなければ採点できない。
            written = probe.run(
                ExecRequest(
                    argv=(
                        "/bin/sh",
                        "-c",
                        "printf ok > /work/write-probe && cat /work/write-probe",
                    ),
                    limits=Limits(cpu_seconds=10, wall_seconds=60.0),
                )
            )

        if not visible:
            raise SandboxUnavailable(
                f"the workspace at {self.workspace_root} is not visible inside the "
                f"container, so submissions would be graded against an empty directory "
                f"(every one of them failing to compile). Mount that path into the "
                f"container runtime, or point {ENV_WORKSPACE_ROOT} at a path it mounts. "
                f"Probe said: {result.stderr.strip()[:200] or result.stdout.strip()[:200]!r}"
            )
        if not written.ok or written.stdout.strip() != "ok":
            raise SandboxUnavailable(
                f"the workspace at {self.workspace_root} is not writable inside the "
                f"container, so a compiled submission has nowhere to go. "
                f"Probe said: {written.stderr.strip()[:200]!r}"
            )

    def timed_out_inside(self, code: int) -> bool:
        """中の `timeout` が上限で止めたか。

        提出が自分で `exit(124)` した場合と区別できないが、`decode_signal` の
        137 と同じく、どちらにしても `ok` は偽で、取り違えの害は小さい。
        """
        return code == TIMEOUT_EXIT_CODE

    def decode_signal(self, code: int) -> str | None:
        """`docker run` の終了コードからシグナル名を引く。

        コンテナの中で主プロセスがシグナル N で死ぬと、`docker run` 自体は
        **128+N で正常終了する**。`subprocess` から見ると負の値にならないので、
        既定の解釈では「シグナルで死んだ」と分からない。

        分からないと何が起きるか: CPU 上限や OOM で殺された提出が
        「終了コード 137 で終わった」＝不正解として扱われる。学習者には
        時間切れが「答えが違う」と表示される。原因の分類を誤ると、
        次の一手も間違ったものになる。

        .. note::

           この規約は曖昧である。提出が自分で `exit(137)` を呼んだ場合と
           区別できない（`docker run --rm` はコンテナを消すので、あとから
           状態を問い合わせられない）。区別を付けるには `--cidfile` と
           `docker inspect` が要るが、得られるのは「稀な意図的 exit(137) を
           時間切れと呼ばない」ことだけで、どちらにしても `ok` は偽である。
           取り違えの害が小さい側に倒してある。
        """
        below = super().decode_signal(code)
        if below is not None:
            return below
        if 128 < code <= 128 + 64:
            try:
                return signal.Signals(code - 128).name
            except ValueError:
                return None
        return None

    def wrap(
        self, argv: list[str], request: ExecRequest, workdir: Path
    ) -> tuple[list[str], dict[str, str]]:
        limits = request.limits
        command = [
            self._binary,
            "run",
            "--rm",
            "--interactive",
            # 名前とラベル。時間切れのとき、クライアントではなくコンテナを
            # 名指しで止めるため（`release`・#410）。
            f"--name={CONTAINER_NAME_PREFIX}{uuid.uuid4().hex}",
            f"--label={CONTAINER_LABEL}=1",
            f"--cpus={CONTAINER_CPUS}",
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
        if self._clear_entrypoint:
            # イメージの ENTRYPOINT を外す（#203）。Dolos のイメージは `dolos` を
            # 入口に持ち、残すと `dolos timeout …` になって何も動かない。
            command.append("--entrypoint=")
        command.append(self._image)
        # 実時間の上限は中で掛ける。起動の時間を持ち時間に数えない（#489）。
        command.extend(
            [
                IN_CONTAINER_TIMEOUT,
                "-k",
                str(TIMEOUT_KILL_AFTER_SECONDS),
                str(limits.wall_seconds),
            ]
        )
        command.extend(argv)

        # docker クライアント自身の環境。中に渡るのは --env で明示した分だけ。
        return command, self._client_env()

    def _client_env(self) -> dict[str, str]:
        return {
            key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST") if key in os.environ
        }

    def release(self, argv: list[str]) -> None:
        """時間切れのあと、コンテナを名指しで止めて消す（#410）。

        `docker run` のクライアントを SIGKILL しても、コンテナは動き続ける。
        sleep やブロックで待つ提出は CPU 上限にも掛からないので、放っておくと
        `--memory` ぶんを抱えたまま残り、繰り返し実行でホストのメモリが尽きる。

        **失敗しても採点は止めない。** 記録を残し、ラベルで棚卸しできる。
        """
        name = next(
            (arg.removeprefix("--name=") for arg in argv if arg.startswith("--name=")), None
        )
        if name is None:
            return
        try:
            subprocess.run(
                [self._binary, "rm", "--force", name],
                capture_output=True,
                timeout=RELEASE_TIMEOUT_SECONDS,
                env=self._client_env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("could not remove sandbox container %s", name, exc_info=True)
