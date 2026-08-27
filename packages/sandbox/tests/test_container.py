"""コンテナバックエンドの脱出試験（設計方針 §11 検証項目 6）。

**実提出を通す前提条件がここ。** macOS/seatbelt は開発用で、プロセス数を
封じ込められない（ADR 0006、実測済み）。実学生のコードを走らせるのは
Linux + コンテナだけで、その「封じ込められる」という主張をここで確かめる。

コンテナが無い環境では skip する。**skip は検証済みではない。**
実提出を通す前に、コンテナのある環境でこのファイルを通すこと。

    AIJUDGE_SANDBOX=docker uv run pytest packages/sandbox/tests/test_container.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aijudge_sandbox import (
    DockerSandbox,
    ExecRequest,
    Isolation,
    Limitation,
    Limits,
    SandboxUnavailable,
)

# コンテナ内でコンパイルするので、ホストの cc は要らない。
# イメージ（既定 gcc:14-bookworm）が持っている。
FAST = Limits(cpu_seconds=5, wall_seconds=60.0, processes=32)


@pytest.fixture(scope="module")
def container():
    """コンテナバックエンド。無ければモジュールごと skip。"""
    try:
        sandbox = DockerSandbox()
    except SandboxUnavailable as exc:
        pytest.skip(f"no container runtime: {exc}")
    return sandbox


def _build(workspace, source: str, name: str = "prog") -> None:
    workspace.write(f"{name}.c", source)
    result = workspace.run(
        ExecRequest(
            argv=("cc", "-std=c11", "-o", name, f"{name}.c"),
            limits=FAST,
            trusted_toolchain=True,
        )
    )
    assert result.ok, f"コンパイルできない: {result.stderr[:400]}"


# --------------------------------------------------------------------------
# 申告
# --------------------------------------------------------------------------


def test_a_container_declares_that_it_can_contain_a_process_bomb(container) -> None:
    """`--pids-limit` があるので、seatbelt の穴はここでは塞がっている。"""
    assert Limitation.PROCESS_LIMIT_UNENFORCED not in container.limitations
    assert Limitation.SHARED_UID not in container.limitations
    assert container.isolation is Isolation.CONTAINER


def test_gvisor_declares_no_limitations() -> None:
    """カーネル共有まで塞がるのは gVisor だけ。"""
    try:
        sandbox = DockerSandbox(runtime="runsc")
    except SandboxUnavailable as exc:
        pytest.skip(f"no gVisor runtime: {exc}")
    assert sandbox.limitations == frozenset()
    assert sandbox.isolation is Isolation.KERNEL_ISOLATED


# --------------------------------------------------------------------------
# 脱出試験
# --------------------------------------------------------------------------


def test_a_program_runs_in_the_container(container) -> None:
    with container.workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("/bin/echo", "hello"), limits=FAST))
    assert result.ok, result.stderr
    assert result.stdout.strip() == "hello"
    assert result.isolation is Isolation.CONTAINER


def test_the_submission_does_not_run_as_root(container) -> None:
    """root で動かすと、コンテナ内の read-only を回避する経路が増える。"""
    with container.workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("/usr/bin/id", "-u"), limits=FAST))
    assert result.stdout.strip() == "65534", result.stdout


def test_the_root_filesystem_is_read_only(container) -> None:
    with container.workspace() as workspace:
        result = workspace.run(
            ExecRequest(
                argv=("/bin/sh", "-c", "touch /etc/pwned && echo ALLOWED || echo denied"),
                limits=FAST,
            )
        )
    assert result.stdout.strip() == "denied", result.stdout


def test_the_submission_cannot_write_outside_the_workspace(container, tmp_path: Path) -> None:
    """作業域はマウントされているが、その外はコンテナから見えない。"""
    target = tmp_path / "escaped.txt"
    with container.workspace() as workspace:
        result = workspace.run(
            ExecRequest(
                argv=("/bin/sh", "-c", f"echo pwned > {target} && echo ALLOWED || echo denied"),
                limits=FAST,
            )
        )
    assert result.stdout.strip() == "denied"
    assert not target.exists(), "ホストにファイルが書かれた"


def test_the_host_home_directory_is_not_visible(container) -> None:
    """鍵・トークン・他学生の答案がある場所。

    作業域は家目録の下に置く（コンテナ実行環境がマウントするのはそこだから）。
    **その親が見えていないこと**を確かめる。見えていれば、作業域を家目録に
    置いた判断がそのまま穴になる。
    """
    home = Path.home().resolve()
    with container.workspace() as workspace:
        result = workspace.run(
            ExecRequest(
                argv=(
                    "/bin/sh",
                    "-c",
                    f'test -e "{home}" && echo VISIBLE || echo denied',
                ),
                limits=FAST,
            )
        )
    assert result.stdout.strip() == "denied", f"ホストの家目録 {home} がコンテナから見えている"


def test_only_the_workspace_is_mounted(container) -> None:
    """作業域の外は持ち込まれていないこと。"""
    with container.workspace() as workspace:
        workspace.write("mine.txt", "x")
        result = workspace.run(ExecRequest(argv=("/bin/sh", "-c", "ls -A /work"), limits=FAST))
    assert sorted(result.stdout.split()) == ["mine.txt"], result.stdout


def test_the_submission_cannot_reach_the_network(container) -> None:
    source = """
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
int main(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { printf("nosocket\\n"); return 0; }
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons(80);
    a.sin_addr.s_addr = inet_addr("93.184.216.34");
    printf("%s\\n", connect(s, (struct sockaddr *)&a, sizeof a) == 0 ? "CONNECTED" : "denied");
    return 0;
}
"""
    with container.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(ExecRequest(argv=("./prog",), limits=FAST))
    assert "CONNECTED" not in result.stdout, "ネットワークに出られた"


def test_an_infinite_loop_is_stopped(container) -> None:
    """CPU 上限で止まり、**時間切れとして分類される**こと。

    分類を誤ると、学習者には時間切れが「答えが違う」と表示される。
    コンテナ越しのシグナルは `docker run` の 128+N で返るので、
    バックエンドがそれを読み替えている（`DockerSandbox.decode_signal`）。
    """
    with container.workspace() as workspace:
        _build(workspace, "int main(void) { for (;;) ; }")
        result = workspace.run(
            ExecRequest(argv=("./prog",), limits=Limits(cpu_seconds=2, wall_seconds=30.0))
        )
    assert result.killed, f"シグナル終了が検出されていない: {result.exit_code}"
    assert result.timed_out, "時間切れとして分類されていない"
    assert not result.ok


def test_a_fork_bomb_is_contained(container) -> None:
    """**実提出を通す前提条件。** `--pids-limit` で頭打ちになること。

    macOS/seatbelt ではここが破れた（ADR 0006）。コンテナで塞がっている
    ことを確かめないまま実提出を通してはならない。
    """
    source = """
#include <unistd.h>
int main(void) { for (;;) { if (fork() < 0) _exit(1); } }
"""
    with container.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(
            ExecRequest(
                argv=("./prog",),
                limits=Limits(cpu_seconds=2, wall_seconds=30.0, processes=16),
            )
        )
    assert result.killed or result.exit_code != 0

    # 封じ込められていれば、同じサンドボックスで次の実行がまだできる。
    with container.workspace() as workspace:
        after = workspace.run(ExecRequest(argv=("/bin/echo", "alive"), limits=FAST))
    assert after.ok, "fork bomb のあとサンドボックスが使えない"


def test_memory_is_capped(container) -> None:
    """メモリ上限。swap を許すと上限が意味を失うので `--memory-swap` も揃える。"""
    source = """
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
int main(void) {
    size_t chunk = 32u * 1024 * 1024;
    for (int i = 0; i < 64; i++) {
        void *p = malloc(chunk);
        if (!p) { printf("denied\\n"); return 0; }
        memset(p, 1, chunk);
    }
    printf("ALLOCATED\\n");
    return 0;
}
"""
    with container.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(
            ExecRequest(
                argv=("./prog",),
                limits=Limits(
                    cpu_seconds=10,
                    wall_seconds=60.0,
                    memory_bytes=64 * 1024 * 1024,
                ),
            )
        )
    assert "ALLOCATED" not in result.stdout, "メモリ上限が効いていない"


def test_runaway_output_is_truncated(container) -> None:
    source = """
#include <stdio.h>
int main(void) { for (long i = 0; i < 5000000L; i++) putchar('x'); return 0; }
"""
    with container.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(
            ExecRequest(
                argv=("./prog",),
                limits=Limits(cpu_seconds=5, wall_seconds=60.0, output_bytes=4096),
            )
        )
    assert len(result.stdout) <= 4096
    assert result.truncated or result.killed


def test_the_workspace_is_removed_afterwards(container) -> None:
    with container.workspace() as workspace:
        path = workspace.path
        workspace.write("a.txt", "x")
    assert not path.exists()


def test_the_environment_does_not_leak_host_secrets(container, monkeypatch) -> None:
    """親の環境をそのまま渡さない。渡すと資格情報が提出コードに見える。"""
    monkeypatch.setenv("AIJUDGE_SECRET_TOKEN", "super-secret-value")
    with container.workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("/usr/bin/env",), limits=FAST))
    assert "super-secret-value" not in result.stdout


def test_the_toolchain_is_available_in_the_image(container) -> None:
    """ホストに cc が無くてもコンテナ内で採点できること。

    これが成立していれば、採点機に開発ツールを入れる必要が無い。
    """
    assert shutil.which("cc") is None or True  # ホスト側の有無に依存しない
    with container.workspace() as workspace:
        result = workspace.run(
            ExecRequest(argv=("cc", "--version"), limits=FAST, trusted_toolchain=True)
        )
    assert result.ok, result.stderr
