"""コンテナバックエンドの脱出試験（設計方針 §11 検証項目 6）。

**実提出を通す前提条件がここ。** macOS/seatbelt は開発用で、プロセス数を
封じ込められない（ADR 0006、実測済み）。実学生のコードを走らせるのは
Linux + コンテナだけで、その「封じ込められる」という主張をここで確かめる。

コンテナが無い環境では skip する。**skip は検証済みではない。**
実提出を通す前に、コンテナのある環境でこのファイルを通すこと。

    AIJUDGE_SANDBOX=docker uv run pytest packages/sandbox/tests/test_container.py -v

**同じ試験を runc と gVisor（runsc）の両方に当てる**（#502）。運用機は gVisor で
採点しているのに、以前は gVisor を通る試験がどこでも自動で走っていなかった。
runsc の版上げや `DockerSandbox` の変更（上限の掛け方・マウント・`--runtime` の
渡し方）で壊れても気づけない。gVisor 側を確かめるには:

    AIJUDGE_SANDBOX=gvisor uv run pytest packages/sandbox/tests/test_container.py -k runsc -v
"""

from __future__ import annotations

import os
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


# 試す runtime。`None` は docker の既定（runc）。
RUNTIMES = {"runc": None, "runsc": "runsc"}

# この runtime が無いとき、skip ではなく失敗にする `AIJUDGE_SANDBOX` の値。
REQUIRED_BY = {"runc": ("docker", "gvisor"), "runsc": ("gvisor",)}


@pytest.fixture(scope="module", params=sorted(RUNTIMES))
def container(request: pytest.FixtureRequest) -> DockerSandbox:
    """コンテナバックエンド。無ければその runtime の試験ごと skip。

    **ただし `AIJUDGE_SANDBOX` でその runtime を名指ししたときは失敗にする**（#429）。
    CI はそう指定して走らせる ── skip にすると、docker や runsc が壊れても CI は
    緑のまま、脱出試験は 1 件も走っていない（skip は検証済みではない）。
    gVisor は runc の上位なので、`gvisor` を名指ししたら両方を要求する。
    """
    name: str = request.param
    try:
        return DockerSandbox(runtime=RUNTIMES[name])
    except SandboxUnavailable as exc:
        if os.environ.get("AIJUDGE_SANDBOX", "").strip().lower() in REQUIRED_BY[name]:
            pytest.fail(f"AIJUDGE_SANDBOX asks for {name} but it is not usable: {exc}")
        pytest.skip(f"no {name} runtime: {exc}")


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


def test_only_gvisor_closes_the_shared_kernel(container, request: pytest.FixtureRequest) -> None:
    """カーネル共有まで塞がるのは gVisor だけ。runc は共有を申告する。

    運用機は `AIJUDGE_SANDBOX_MIN=kernel_isolated` で、この申告が隔離の強さの
    判定そのものになる（#449）。runsc なのに申告が弱いと、運用機は採点を拒む。
    """
    if request.node.callspec.params["container"] == "runsc":
        assert container.limitations == frozenset()
        assert container.isolation is Isolation.KERNEL_ISOLATED
    else:
        assert container.limitations == frozenset({Limitation.SHARED_KERNEL})
        assert container.isolation is Isolation.CONTAINER


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


def _record_names(container, monkeypatch) -> list[str]:
    """このテストが起動したコンテナの名前を集める。

    ラベル（`CONTAINER_LABEL`）で数えると、並列に流れている他のテストの
    コンテナまで数えてしまう（#489）。名前は `wrap` が 1 回ごとに振る。
    """
    names: list[str] = []
    wrap = container.wrap

    def recording(argv, request, workdir):
        command, env = wrap(argv, request, workdir)
        names.extend(arg.removeprefix("--name=") for arg in command if arg.startswith("--name="))
        return command, env

    monkeypatch.setattr(container, "wrap", recording)
    return names


def test_a_sleeping_submission_does_not_outlive_its_timeout(container, monkeypatch) -> None:
    """**時間切れの後にコンテナが残らない**（#410）。

    CPU を使わずに待つ提出は `--ulimit=cpu` に掛からない。壁時計で
    `docker run` のクライアントを殺しても、コンテナは `--memory` ぶんを
    抱えたまま動き続けていた ── 試し実行を繰り返せばホストのメモリが尽きる。
    いまは中の `timeout` が止める（#489）ので、コンテナは自分で終わる。
    """
    import subprocess

    names = _record_names(container, monkeypatch)
    with container.workspace() as workspace:
        result = workspace.run(
            ExecRequest(argv=("/bin/sleep", "120"), limits=Limits(cpu_seconds=5, wall_seconds=3.0))
        )
    assert result.timed_out
    assert len(names) == 1
    alive = subprocess.run(
        ["docker", "ps", "--quiet", "--filter", f"name=^{names[0]}$"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert alive == [], f"時間切れの後もコンテナが残っている: {names[0]}"


def test_starting_the_container_is_not_counted_against_the_limit(container) -> None:
    """持ち時間がコンテナの起動より短くても、すぐ終わる提出は時間切れにならない（#489）。

    以前は `docker run` のクライアントに上限を掛けていたので、0.1 秒の上限は
    起動だけで使い切った（`docker run` は runc でも 0.3 秒前後、gVisor はさらに
    重い）。0.5 秒では手元の速い機械だと起動が間に合い、直す前でも通ってしまった。
    """
    with container.workspace() as workspace:
        result = workspace.run(
            ExecRequest(argv=("/bin/true",), limits=Limits(cpu_seconds=1, wall_seconds=0.1))
        )
    assert result.ok, result
    assert not result.timed_out


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
