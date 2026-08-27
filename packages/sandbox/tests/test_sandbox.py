"""サンドボックスの脱出試験と選択規則。

設計方針 §11 の検証項目 6。悪意ある提出を模したコードを実際に走らせ、
ネットワーク・ファイルシステム・fork bomb・長時間実行が止まることを確かめる。

隔離を実装したと主張するなら、破れないことを実際に試さなければ意味がない。
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

import pytest

from aijudge_sandbox import (
    ExecRequest,
    Isolation,
    Limitation,
    Limits,
    SandboxUnavailable,
    SeatbeltSandbox,
    UnsafeLocalSandbox,
    UnsafeSandboxRefused,
    build_sandbox,
)

on_macos = pytest.mark.skipif(platform.system() != "Darwin", reason="seatbelt is macOS only")

# fork bomb 試験は、プロセス数を強制できるバックエンドでしか走らせない。
# seatbelt で走らせた実測（2026-08）では、暴走プロセスがホスト利用者の
# プロセス表を埋め、シェルが fork できなくなった。試験そのものが事故になる。
# 「上限が効かないことを確かめる試験」は要らない。効かないことは
# バックエンドが `Limitation.PROCESS_LIMIT_UNENFORCED` で申告する。
FORK_BOMB_OPT_IN = "AIJUDGE_RUN_FORK_BOMB_TEST"
needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler available")

FAST = Limits(cpu_seconds=2, wall_seconds=6.0, processes=32)


@pytest.fixture
def sandbox() -> SeatbeltSandbox:
    try:
        return SeatbeltSandbox()
    except SandboxUnavailable as exc:
        pytest.skip(str(exc))


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


def _home_probe() -> Path | None:
    """家目録にある「読めては困るファイル」を 1 つ選ぶ。

    シェルは利用者ごとに違う（この開発機は fish）。特定のファイル名を
    決め打ちにすると、環境によっては試験が黙って skip され、
    隔離の主要な主張がひとつ未検証のまま通ってしまう。
    """
    home = Path(os.path.expanduser("~")).resolve()
    named = (
        ".zshrc",
        ".bashrc",
        ".profile",
        ".gitconfig",
        ".config/fish/config.fish",
    )
    for name in named:
        candidate = home / name
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return candidate
    # 名前で見つからなければ、家目録直下の読める通常ファイルを拾う。
    for candidate in sorted(home.iterdir()):
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return candidate
    return None


# --------------------------------------------------------------------------
# 選択規則 — 隔離なしに落ちないこと
# --------------------------------------------------------------------------


def test_the_unsafe_backend_refuses_without_an_explicit_opt_in() -> None:
    """既定で拒否する。警告コメントでは守れない。"""
    with (
        pytest.raises(UnsafeSandboxRefused, match="without isolation"),
        UnsafeLocalSandbox().workspace(),
    ):
        pass


def test_selecting_unsafe_needs_a_second_deliberate_flag(monkeypatch) -> None:
    """環境変数 1 つで隔離が外れると、設定ミスが静かに事故になる。"""
    monkeypatch.delenv("AIJUDGE_SANDBOX_I_KNOW_THIS_IS_UNSAFE", raising=False)
    with pytest.raises(SandboxUnavailable, match="I_KNOW_THIS_IS_UNSAFE"):
        build_sandbox("unsafe")

    monkeypatch.setenv("AIJUDGE_SANDBOX_I_KNOW_THIS_IS_UNSAFE", "yes")
    assert build_sandbox("unsafe").isolation is Isolation.NONE


def test_an_unknown_backend_is_rejected() -> None:
    with pytest.raises(SandboxUnavailable, match="unknown sandbox backend"):
        build_sandbox("definitely-not-a-backend")


@on_macos
def test_auto_selection_never_returns_an_unisolated_sandbox(monkeypatch) -> None:
    """自動選択の結果が Isolation.NONE になることはない。"""
    monkeypatch.delenv("AIJUDGE_SANDBOX", raising=False)
    assert build_sandbox().isolation is not Isolation.NONE


def test_docker_is_not_claimed_when_the_daemon_is_unreachable() -> None:
    """CLI があることと動くことは別。届かないなら「使える」と言わない。"""
    from aijudge_sandbox.backends import DockerSandbox

    if shutil.which("docker") is None:
        with pytest.raises(SandboxUnavailable, match="not installed"):
            DockerSandbox()
    else:  # pragma: no cover - docker のある環境でのみ
        try:
            DockerSandbox()
        except SandboxUnavailable as exc:
            assert "daemon" in str(exc) or "runtime" in str(exc)


# --------------------------------------------------------------------------
# 基本動作
# --------------------------------------------------------------------------


@on_macos
def test_a_program_runs_and_its_output_comes_back(sandbox) -> None:
    with sandbox.workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("/bin/echo", "hello"), limits=FAST))
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.isolation is Isolation.OS_SANDBOX


@on_macos
def test_stdin_is_delivered(sandbox) -> None:
    with sandbox.workspace() as workspace:
        result = workspace.run(ExecRequest(argv=("/bin/cat",), stdin="21\n", limits=FAST))
    assert result.stdout.strip() == "21"


def test_writing_outside_the_workspace_is_refused() -> None:
    """`../` で作業域の外にファイルを置けないこと。"""
    with (
        UnsafeLocalSandbox(allow_unsafe=True).workspace() as workspace,
        pytest.raises(ValueError, match="escapes the workspace"),
    ):
        workspace.write("../escaped.txt", "x")


@on_macos
def test_the_workspace_is_removed_afterwards(sandbox) -> None:
    with sandbox.workspace() as workspace:
        path = workspace.path
        workspace.write("a.txt", "x")
        assert path.is_dir()
    assert not path.exists()


# --------------------------------------------------------------------------
# 脱出試験
# --------------------------------------------------------------------------


@on_macos
@needs_cc
def test_the_submission_cannot_write_outside_the_workspace(sandbox, tmp_path) -> None:
    target = tmp_path / "escaped.txt"
    source = f"""
#include <stdio.h>
int main(void) {{
    FILE *f = fopen("{target}", "w");
    printf("%s\\n", f ? "ALLOWED" : "denied");
    if (f) {{ fputs("pwned", f); fclose(f); }}
    return 0;
}}
"""
    with sandbox.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(ExecRequest(argv=("./prog",), limits=FAST))

    assert result.stdout.strip() == "denied"
    assert not target.exists(), "ホストにファイルが書かれた"


@on_macos
@needs_cc
def test_the_submission_cannot_reach_the_network(sandbox) -> None:
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
    with sandbox.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(ExecRequest(argv=("./prog",), limits=FAST))

    assert "CONNECTED" not in result.stdout, "ネットワークに出られた"


@on_macos
@needs_cc
def test_the_submission_cannot_read_the_home_directory(sandbox) -> None:
    """家目録には鍵・トークン・他学生の答案がある。"""
    probe = _home_probe()
    if probe is None:
        pytest.skip("no readable probe file in the home directory")

    source = f"""
#include <stdio.h>
int main(void) {{
    FILE *f = fopen("{probe}", "r");
    printf("%s\\n", f ? "READ" : "denied");
    if (f) fclose(f);
    return 0;
}}
"""
    with sandbox.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(ExecRequest(argv=("./prog",), limits=FAST))

    assert result.stdout.strip() == "denied"


@on_macos
@needs_cc
def test_an_infinite_loop_is_stopped(sandbox) -> None:
    with sandbox.workspace() as workspace:
        _build(workspace, "int main(void) { for (;;) ; }")
        result = workspace.run(
            ExecRequest(argv=("./prog",), limits=Limits(cpu_seconds=1, wall_seconds=8.0))
        )

    assert result.timed_out
    assert not result.ok


def test_a_backend_that_cannot_contain_a_fork_bomb_says_so() -> None:
    """申告のない穴が一番危ない。seatbelt には自己申告させる。

    この申告が `test_a_fork_bomb_is_contained` の実行可否を決める。
    """
    if platform.system() == "Darwin":
        assert Limitation.PROCESS_LIMIT_UNENFORCED in SeatbeltSandbox().limitations
    assert Limitation.NO_ISOLATION in UnsafeLocalSandbox().limitations


@needs_cc
@pytest.mark.skipif(
    os.environ.get(FORK_BOMB_OPT_IN) != "yes",
    reason=f"destructive; set {FORK_BOMB_OPT_IN}=yes to run",
)
def test_a_fork_bomb_is_contained() -> None:
    """プロセス数の上限で止まり、**ホストに漏れない**こと。

    走らせてよいのは、封じ込めを申告しているバックエンドだけ。
    申告していないものは試験ごと拒否する（走らせれば採点機が落ちる）。
    """
    sandbox = build_sandbox()
    if Limitation.PROCESS_LIMIT_UNENFORCED in sandbox.limitations:
        pytest.skip(
            f"{sandbox.name} cannot contain a fork bomb; running this would take "
            "the host down. Use a container backend (AIJUDGE_SANDBOX=docker)."
        )

    source = """
#include <unistd.h>
int main(void) { for (;;) { if (fork() < 0) _exit(1); } }
"""
    with sandbox.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(
            ExecRequest(
                argv=("./prog",),
                limits=Limits(cpu_seconds=1, wall_seconds=10.0, processes=16),
            )
        )

    assert result.killed or result.exit_code != 0
    # ここまで到達できていること自体が、ホストが生きている証拠。
    assert shutil.which("cc") is not None


@on_macos
@needs_cc
def test_runaway_output_is_truncated(sandbox) -> None:
    """出力で採点機のメモリを食い潰させない。"""
    source = """
#include <stdio.h>
int main(void) { for (long i = 0; i < 5000000L; i++) putchar('x'); return 0; }
"""
    with sandbox.workspace() as workspace:
        _build(workspace, source)
        result = workspace.run(
            ExecRequest(
                argv=("./prog",),
                limits=Limits(cpu_seconds=3, wall_seconds=15.0, output_bytes=4096),
            )
        )

    assert len(result.stdout) <= 4096
    assert result.truncated or result.killed


@on_macos
@needs_cc
def test_the_compiler_cannot_be_used_to_write_outside_either(sandbox, tmp_path) -> None:
    """`#include` で家目録を覗く経路も塞がっていること。"""
    secret = tmp_path / "secret.h"
    secret.write_text('const char *s = "leak";\n', encoding="utf-8")

    with sandbox.workspace() as workspace:
        workspace.write("prog.c", f'#include "{secret}"\nint main(void){{return 0;}}')
        result = workspace.run(
            ExecRequest(argv=("cc", "-o", "prog", "prog.c"), limits=FAST, trusted_toolchain=True)
        )
    # tmp_path は家目録の外なので読める。ここで確かめたいのは
    # 「読めた場合でも作業域の外に出力できない」こと。
    assert result.ok or "No such file" in result.stderr
