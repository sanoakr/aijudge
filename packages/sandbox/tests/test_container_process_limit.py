"""コンテナのプロセス数の上限（`--pids-limit`）が実際に効いたことを見分ける（#703）。

`test_container.py` の fork bomb 試験は `result.killed or result.exit_code != 0` を
見るだけで、CPU 時間の上限（`--ulimit=cpu`）・中の `timeout`（124）・メモリの
上限（OOM）のどれで止まっても真になる。`--pids-limit` を消しても通りうるので、
ADR 0006 が実提出を通す前提とした性質（プロセス数の頭打ち）を確かめていなかった。

ここでは提出を「fork に失敗したら、それまでに増やした数を出力して `exit(1)`」に
する。上限が効いていれば fork は**すぐに**失敗し、出力した数は上限以下になる。
上限が無ければ fork は失敗せず、歯止め（`RUNAWAY_GUARD`）に達して別の終了コードで
終わる ── 歯止めは、上限が外れたときにこの試験そのものが fork bomb にならない
ためのもの。

docker が無ければ skip、`AIJUDGE_SANDBOX=docker` / `gvisor` を名指ししたときは
失敗にする（`test_container.py` の `container` と同じ作法・#429）。CI の本体の段
（`AIJUDGE_SANDBOX=docker`）で流れる。

**runc だけで試す。** gVisor では `--pids-limit` が Sentry 自身のスレッドにも
掛かり（#502）、16 ではサンドボックスが立ち上がらないことがある。gVisor の側は
`test_container.py` の fork bomb 試験が CI の gVisor の段で見ている。
"""

from __future__ import annotations

import os

import pytest

from aijudge_sandbox import DockerSandbox, ExecRequest, Limits, SandboxUnavailable

# コンパイルに使う上限（`test_container.py` の FAST と同じ）。
BUILD = Limits(cpu_seconds=5, wall_seconds=60.0, processes=64)
# 試すプロセス数の上限。`test_container.py` の fork bomb 試験と同じ値。
PROCESS_LIMIT = 16
RUN = Limits(cpu_seconds=2, wall_seconds=30.0, processes=PROCESS_LIMIT)
# 上限が効かないときに fork をやめる数。上限よりずっと大きく、ホストを
# 巻き込むほどではない。
RUNAWAY_GUARD = 512
# fork が失敗したときの終了コードと、歯止めに達したときの終了コード。
FORK_FAILED = 1
GUARD_REACHED = 2

SOURCE = f"""
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <sys/types.h>
#include <unistd.h>
int main(void) {{
    int spawned = 0;
    for (;;) {{
        pid_t pid = fork();
        if (pid < 0) {{
            printf("%d\\n", spawned);
            fflush(stdout);
            return {FORK_FAILED};
        }}
        if (pid == 0) {{
            pause();
            _exit(0);
        }}
        if (++spawned >= {RUNAWAY_GUARD}) {{
            printf("%d\\n", spawned);
            fflush(stdout);
            return {GUARD_REACHED};
        }}
    }}
}}
"""


@pytest.fixture(scope="module")
def runc() -> DockerSandbox:
    try:
        return DockerSandbox()
    except SandboxUnavailable as exc:
        if os.environ.get("AIJUDGE_SANDBOX", "").strip().lower() in ("docker", "gvisor"):
            pytest.fail(f"AIJUDGE_SANDBOX asks for a container but runc is not usable: {exc}")
        pytest.skip(f"no runc runtime: {exc}")


def test_fork_fails_at_the_process_limit(runc: DockerSandbox) -> None:
    with runc.workspace() as workspace:
        workspace.write("prog.c", SOURCE)
        built = workspace.run(
            ExecRequest(
                argv=("cc", "-std=c11", "-o", "prog", "prog.c"),
                limits=BUILD,
                trusted_toolchain=True,
            )
        )
        assert built.ok, f"コンパイルできない: {built.stderr[:400]}"
        result = workspace.run(ExecRequest(argv=("./prog",), limits=RUN))

    # 止めたのは fork の失敗であって、時間切れや CPU・メモリの上限ではない。
    assert not result.timed_out, result
    assert result.signal_name is None, result
    assert result.exit_code == FORK_FAILED, (result.exit_code, result.stdout, result.stderr)
    spawned = int(result.stdout.strip())
    # 中の `timeout` と提出物自身も数に入るので、増やせた数は上限より少ない。
    assert 1 <= spawned <= PROCESS_LIMIT, spawned
