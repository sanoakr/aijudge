"""伴走プロセスによる採点を検証する（ADR 0008）。

**教材と同じ形で試す。** ネットワーク演習の `echoServer.py`（伴走サーバ）と
`echoClient2.py`（クライアント課題の参照解答）、`httpServer2.py`（サーバ課題の
参照解答）の骨格をそのまま使う。教材そのものはリポジトリ外（個人情報を含む
ディレクトリ）にあるので、同じ振る舞いのものをここに置く。

コンテナ実行環境が無い機では skip する。**skip は検証済みではない。**
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorStatus,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
    new_id,
)
from aijudge_core import TestCase as SpecCase
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_eval_network_test_runner import EVALUATOR_ID, NetworkTestRunner
from aijudge_grading.protocol import EvaluationRequest
from aijudge_sandbox import DockerSandbox, SandboxUnavailable

NOW = datetime(2026, 8, 28, tzinfo=UTC)
PORT = 50007

# --- 教材と同じ振る舞いの伴走サーバ ---
ECHO_SERVER = """\
import socket
HOST = ''
PORT = 50007
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
    s.bind((HOST, PORT))
    while True:
        s.listen(1)
        conn, addr = s.accept()
        with conn:
            print('Connected by', addr, flush=True)
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                print('Received', repr(data), flush=True)
                conn.sendall(data)
"""

# --- クライアント課題の参照解答（echoClient2.py 相当）---
ECHO_CLIENT_OK = """\
import socket
host = input()
port = int(input())
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.connect((host, port))
    send = b'Hello, world'
    s.sendall(send)
    print('Send', repr(send))
    data = s.recv(1024)
    print('Received', repr(data))
"""

# ホスト・ポートを標準入力から読まず、定数のまま（課題の要求を満たさない）。
ECHO_CLIENT_HARDCODED = """\
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.connect(('127.0.0.1', 50007))
    s.sendall(b'Bye')
    print('Send', repr(b'Bye'))
    print('Received', repr(s.recv(1024)))
"""

ECHO_CLIENT_NEVER_CONNECTS = "print('Send nothing')\n"

# --- サーバ課題の参照解答（httpServer2.py 相当）---
HTTP_SERVER_OK = """\
import socket
while True:
    port = int(input())
    if port < 1024:
        print('ポート番号は1024以上しか使えません', flush=True)
    else:
        break
file = input()
print(f'port={port}, file={file}', flush=True)
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
    s.bind(('', port))
    while True:
        s.listen(1)
        conn, addr = s.accept()
        with conn:
            print('Connected by,', addr, flush=True)
            data = conn.recv(1024)
            print('Received:', data.decode(errors='replace'), flush=True)
            body = open(file, 'rb').read()
            conn.sendall(b'HTTP/1.1 200 OK\\n\\n' + body)
"""

HTTP_SERVER_NEVER_BINDS = "print('starting')\n"

# --- サーバ課題の伴走クライアント（httpCheckClient 相当）---
HTTP_CLIENT = """\
import socket
port = int(input())
name = input()
req = f'GET /{name} HTTP/1.1\\nHost: localhost\\n\\n'.encode()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.connect(('127.0.0.1', port))
    s.sendall(req)
    print(s.recv(4096).decode(errors='replace'))
"""

PAGE = "<html><body>これは server.html です。</body></html>"


@pytest.fixture(scope="module")
def sandbox():
    try:
        return DockerSandbox()
    except SandboxUnavailable as exc:
        pytest.skip(f"no container runtime: {exc}")


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="correctness",
        title="通信の正しさ",
        description="伴走プロセスとの通信が仕様どおりか",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="動かない", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="一部", score_ratio=0.34),
            RubricLevel(level=2, label="概ね", descriptor="大半", score_ratio=0.67),
            RubricLevel(level=3, label="達成", descriptor="すべて", score_ratio=1.0),
        ),
        evaluator_id=EVALUATOR_ID,
    )


def _request(source: str, cases: tuple[SpecCase, ...]) -> EvaluationRequest:
    payload = source.encode()
    submission_id = SubmissionId(new_id("sub"))
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        filename="main.py",
        storage_key="mem",
        content_hash="sha256:" + "0" * 64,
        byte_size=len(payload),
        created_at=NOW,
    )
    criterion = _criterion()
    task = TaskVersion(
        id=TaskVersionId("tsv_" + "2" * 32),
        task_id=TaskId("tsk_" + "3" * 32),
        version=1,
        subject_profile="net_python",
        statement="## echoClient2.py\n\nホストとポートを読んで接続しなさい。",
        criteria=(criterion,),
        test_cases=cases,
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "4" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=submission_id,
        task_version_id=task.id,
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=NOW,
        submitted_at=NOW,
    )
    return EvaluationRequest(
        task_version=task,
        submission=submission,
        artifact_contents={artifact.id: payload},
        criterion=criterion,
        test_cases=cases,
        timeout_seconds=60.0,
        options={"language": "python", "ready_timeout_seconds": 6.0},
    )


def _client_case(name: str = "case1") -> SpecCase:
    return SpecCase(
        name=name,
        evaluator_id=EVALUATOR_ID,
        payload={
            "role": "client",
            "companion": ECHO_SERVER,
            "companion_name": "echoServer.py",
            "port": PORT,
            # 課題の入力にはホストとポートが書かれている。伴走プロセスの
            # 値に差し替えるのが `{host}` / `{port}`。
            "input": "{host}\n{port}\n",
            "expected_contains": ["Send b'Hello, world'", "Received b'Hello, world'"],
        },
    )


def _server_case(name: str = "case1") -> SpecCase:
    return SpecCase(
        name=name,
        evaluator_id=EVALUATOR_ID,
        payload={
            "role": "server",
            "companion": HTTP_CLIENT,
            "companion_name": "httpCheckClient.py",
            "port": 8080,
            "input": "{port}\nserver.html\n",
            "companion_input": "{port}\nserver.html\n",
            "fixtures": {"server.html": PAGE},
            # サーバの出力には接続元の一時ポートが混ざるので部分一致で見る。
            "expected_contains": ["port=8080, file=server.html", "Received: GET /server.html"],
            "companion_expected_contains": ["これは server.html です"],
        },
    )


def _run(sandbox, source: str, cases: tuple[SpecCase, ...]):
    return NetworkTestRunner(sandbox).evaluate(_request(source, cases))


# --------------------------------------------------------------------------
# クライアント課題
# --------------------------------------------------------------------------


def test_a_correct_client_passes(sandbox) -> None:
    """伴走サーバを立て、提出が接続して往復できること。"""
    outcome = _run(sandbox, ECHO_CLIENT_OK, (_client_case(),))
    assert outcome.status is EvaluatorStatus.OK, outcome.error
    assert outcome.scores[0].score_ratio == pytest.approx(1.0)
    assert outcome.scores[0].conclusive


def test_a_client_that_ignores_stdin_fails(sandbox) -> None:
    """ホストとポートを標準入力から読むことが課題の要求。

    定数のまま繋いでも「たまたま同じポート」なら通ってしまうので、
    送る内容の違いで落ちることを確かめる。
    """
    outcome = _run(sandbox, ECHO_CLIENT_HARDCODED, (_client_case(),))
    assert outcome.scores[0].score_ratio == pytest.approx(0.0)
    assert "Hello, world" in outcome.scores[0].rationale


def test_a_client_that_never_connects_fails(sandbox) -> None:
    outcome = _run(sandbox, ECHO_CLIENT_NEVER_CONNECTS, (_client_case(),))
    assert outcome.scores[0].score_ratio == pytest.approx(0.0)


def test_partial_credit_across_cases(sandbox) -> None:
    """一部のケースだけ通る提出に部分点が出ること。"""
    bad = _client_case("case2")
    bad = bad.model_copy(
        update={"payload": dict(bad.payload) | {"expected_contains": ["この文字列は絶対に出ない"]}}
    )
    outcome = _run(sandbox, ECHO_CLIENT_OK, (_client_case("case1"), bad))
    assert outcome.scores[0].score_ratio == pytest.approx(0.5)


# --------------------------------------------------------------------------
# サーバ課題 — Sharif Judge では採点できなかったもの
# --------------------------------------------------------------------------


def test_a_correct_server_passes(sandbox) -> None:
    """提出が待ち受け、伴走クライアントが繋いで応答を確かめる。"""
    outcome = _run(sandbox, HTTP_SERVER_OK, (_server_case(),))
    assert outcome.status is EvaluatorStatus.OK, outcome.error
    assert outcome.scores[0].score_ratio == pytest.approx(1.0), outcome.scores[0].rationale


def test_a_server_that_never_listens_is_reported_as_such(sandbox) -> None:
    """「出力が違う」ではなく「待ち受けを始めなかった」と伝える。

    原因が分からないと次の一手も分からない。
    """
    outcome = _run(sandbox, HTTP_SERVER_NEVER_BINDS, (_server_case(),))
    assert outcome.scores[0].score_ratio == pytest.approx(0.0)
    assert "待ち受け" in outcome.scores[0].rationale
    cases = outcome.raw_output["cases"]
    assert cases[0]["reason"] == "not_listening"


def test_the_companion_output_is_checked_too(sandbox) -> None:
    """提出の出力だけ見ると、応答を返さないサーバが通ってしまう。"""
    silent = HTTP_SERVER_OK.replace(
        "            conn.sendall(b'HTTP/1.1 200 OK\\n\\n' + body)", "            pass"
    )
    outcome = _run(sandbox, silent, (_server_case(),))
    assert outcome.scores[0].score_ratio == pytest.approx(0.0)


def test_the_submission_and_companion_outputs_are_not_swapped(sandbox) -> None:
    """役割を取り違えると、伴走プロセスの出力で提出を採点してしまう。

    伴走クライアントが必ず出力する文字列を提出の期待値に置いて、
    それでは通らないことを確かめる。
    """
    case = _server_case()
    case = case.model_copy(
        update={
            "payload": dict(case.payload)
            | {"expected_contains": ["HTTP/1.1 200 OK"], "companion_expected_contains": []}
        }
    )
    outcome = _run(sandbox, HTTP_SERVER_OK, (case,))
    assert outcome.scores[0].score_ratio == pytest.approx(0.0), (
        "伴走プロセスの出力を提出の出力として照合している"
    )


# --------------------------------------------------------------------------
# 宣言の誤り — 0 点にしない
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"role": "peer"},
        {"companion": ""},
        {"port": 80},
        {"port": "8080"},
    ],
)
def test_a_malformed_case_fails_the_evaluator_not_the_learner(sandbox, override) -> None:
    """宣言の誤りは学習者の責任ではない。0 点ではなく評価器の失敗にする。"""
    case = _client_case()
    case = case.model_copy(update={"payload": dict(case.payload) | override})
    outcome = _run(sandbox, ECHO_CLIENT_OK, (case,))
    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.scores == ()


def test_no_criterion_means_skipped(sandbox) -> None:
    request = _request(ECHO_CLIENT_OK, (_client_case(),))
    request = request.model_copy(update={"criterion": None, "task_version": request.task_version})
    stripped = request.task_version.criteria[0].model_copy(update={"evaluator_id": "other"})
    request = request.model_copy(
        update={
            "task_version": request.task_version.model_copy(update={"criteria": (stripped,)}),
            "criterion": None,
        }
    )
    outcome = NetworkTestRunner(sandbox).evaluate(request)
    assert outcome.status is EvaluatorStatus.SKIPPED
