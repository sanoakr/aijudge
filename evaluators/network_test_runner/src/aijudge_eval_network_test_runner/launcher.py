"""サンドボックス内で走る起動スクリプトの生成。

**このファイルの中身はサンドボックスの中で実行される。** 採点機の側では
動かない。文字列として組み立て、作業域に書き出す。

なぜ起動スクリプトが要るか。伴走プロセスと提出物は**同時に**動いていなければ
ならない。`Workspace.run` は 1 コマンドを終了まで走らせる契約なので、
2 つを並べるにはサンドボックスの中に段取りを持ち込むしかない。

**同一コンテナ内の 2 プロセスにする**のが要点。別コンテナにしてネットワークで
繋ぐ案もあるが、同一コンテナなら:

- `--network=none` のまま loopback で通信できる（実測で確認）。外部への
  到達は塞がったままで、隔離を課題の都合で緩めない
- 課題文が「同一 PC 上で echoServer.py を実行」と書いている状況と一致する
- サンドボックスの契約（`ExecRequest` / `Workspace`）を変えずに済む

出力は JSON で返す。標準出力を素で混ぜると、提出物の出力と段取りの出力が
区別できない。
"""

from __future__ import annotations

import json

# 待ち受け開始を待つ間隔と上限。上限を超えたら「起動しなかった」として返す。
POLL_SECONDS = 0.05

LAUNCHER_NAME = ".aijudge-launch.py"

_TEMPLATE = '''\
"""aiJudge が生成した起動スクリプト。提出物ではない。"""
import json
import socket
import subprocess
import sys
import time

PORT = {port}
READY_TIMEOUT = {ready_timeout}
RUN_TIMEOUT = {run_timeout}
BACKGROUND_ARGV = {background_argv}
BACKGROUND_STDIN = {background_stdin}
FOREGROUND_ARGV = {foreground_argv}
FOREGROUND_STDIN = {foreground_stdin}
BACKGROUND_ROLE = {background_role}


def wait_for_port(port, deadline):
    """待ち受けが始まるまで待つ。

    起動を待たずに接続すると、提出物が遅いだけで「繋がらない」と判定する。
    逆に固定時間 sleep すると、速い提出でも毎回その時間を払う。
    """
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep({poll})
    return False


def drain(process):
    try:
        out, err = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate(timeout=5)
    return out or "", err or ""


result = {{"ready": False, "background_role": BACKGROUND_ROLE}}

background = subprocess.Popen(
    BACKGROUND_ARGV,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
try:
    if BACKGROUND_STDIN:
        # 待ち受け側は入力を読んでから bind することがある（ポート番号を
        # 標準入力で受け取る課題）。閉じずに書き込む。
        background.stdin.write(BACKGROUND_STDIN)
        background.stdin.flush()

    result["ready"] = wait_for_port(PORT, time.monotonic() + READY_TIMEOUT)

    if not result["ready"]:
        # 起動しなかった。前景を走らせても意味が無いので、ここで返す。
        background.kill()
        out, err = drain(background)
        result["background"] = {{"stdout": out, "stderr": err, "returncode": background.returncode}}
        print(json.dumps(result))
        sys.exit(0)

    try:
        completed = subprocess.run(
            FOREGROUND_ARGV,
            input=FOREGROUND_STDIN,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )
        result["foreground"] = {{
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "timed_out": False,
        }}
    except subprocess.TimeoutExpired as exc:
        result["foreground"] = {{
            "stdout": exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            "returncode": None,
            "timed_out": True,
        }}
finally:
    background.kill()
    out, err = drain(background)
    result["background"] = {{"stdout": out, "stderr": err, "returncode": background.returncode}}

print(json.dumps(result))
'''


def render(
    *,
    port: int,
    background_argv: tuple[str, ...],
    background_stdin: str,
    foreground_argv: tuple[str, ...],
    foreground_stdin: str,
    background_role: str,
    ready_timeout: float,
    run_timeout: float,
) -> str:
    """起動スクリプトを組み立てる。

    引数は `repr` で埋め込む。文字列連結で組み立てると、提出物のファイル名や
    入力に引用符が入った時点で壊れる。
    """
    return _TEMPLATE.format(
        port=int(port),
        ready_timeout=float(ready_timeout),
        run_timeout=float(run_timeout),
        background_argv=repr(list(background_argv)),
        background_stdin=repr(background_stdin),
        foreground_argv=repr(list(foreground_argv)),
        foreground_stdin=repr(foreground_stdin),
        background_role=repr(background_role),
        poll=POLL_SECONDS,
    )


def parse(stdout: str) -> dict:
    """起動スクリプトの出力を読む。壊れていれば例外。

    黙って空の結果にすると、段取りが失敗したのに「提出物が何も出力しなかった」
    として 0 点が付く。
    """
    text = stdout.strip()
    if not text:
        raise ValueError("launcher produced no output")
    # 提出物が標準出力に書いた分が前に付くことはない（別プロセスで捕まえている）。
    # それでも最後の行を取るのは、Python の警告が先に出る場合に備えて。
    last = text.splitlines()[-1]
    try:
        return json.loads(last)
    except json.JSONDecodeError as exc:
        raise ValueError(f"launcher output is not JSON: {last[:200]!r}") from exc
