"""`{host}` を埋める宛先は 1 か所で決める（2026-10-02）。

評価器の置き換え・待ち受けの確認・確定画面の表示の 3 か所に同じ値を書いていたので、
片方だけ変えると採点で渡していない宛先が画面に出た。サンドボックスなしで確かめる。
"""

from __future__ import annotations

import ast

from aijudge_eval_network_test_runner import COMPANION_HOST, launcher


def test_the_launcher_waits_on_the_same_host_the_input_is_filled_with() -> None:
    script = launcher.render(
        port=50007,
        background_argv=("python3", "-I", "echoServer.py"),
        background_stdin="",
        foreground_argv=("python3", "-I", "main.py"),
        foreground_stdin="127.0.0.1\n50007\n",
        background_role="companion",
        ready_timeout=5.0,
        run_timeout=5.0,
    )

    tree = ast.parse(script)  # 起動スクリプトとして組み上がっていること
    hosts = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "HOST" for target in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert hosts == [COMPANION_HOST]
    assert "connect_ex((HOST, port))" in script


def test_the_review_page_fills_the_input_with_the_runners_host() -> None:
    from aijudge_reviewconsole.io_results import NETWORK_HOST

    assert NETWORK_HOST == COMPANION_HOST
