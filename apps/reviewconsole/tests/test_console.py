"""レビューコンソールの規則を固定する。

最重要は「blind 画面に AI の判定が漏れていないこと」。
CSS で隠すのでは不十分なので、レスポンス本文を直接検査している。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_grading import EvaluatorRegistry
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_reviewconsole import Console, ReviewStore, create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_GOLDEN = REPO_ROOT / "evals" / "golden"
PROFILES = REPO_ROOT / "subjects"

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

PROFILE_SAMPLES = 3

# AI は段階 3（明快）と判定する。例のデータの教員採点は 1 なので必ず食い違う。
AI_SAYS_3 = (
    '{"level": 3, "evidence": [{"start_line": 5, "end_line": 5, '
    '"quote": "int b = 0, c = 0, d = 0;"}], '
    '"rationale": "AIRATIONALEMARKER 変数名は明快で構造も追えます。"}'
)

ENTRY = "cs_intro_c/example-task/s001.c"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """例のゴールデンセットを複製し、既存の採点を消して未レビューにする。"""
    root = tmp_path / "golden"
    shutil.copytree(EXAMPLE_GOLDEN, root)
    (root / "cs_intro_c" / "example-task" / "marks" / "s001.yaml").unlink()
    return root


def _client(root: Path, responses: list[str] | None = None) -> TestClient:
    scripted = [r for r in (responses or [AI_SAYS_3]) for _ in range(PROFILE_SAMPLES)]
    registry = EvaluatorRegistry().load_installed()
    registry.replace(RubricAiJudge(LlmGateway(ScriptedProvider(scripted)), model="stub"))
    console = Console(ReviewStore(root), PROFILES, registry=registry, marker="tester")
    return TestClient(create_app(console))


def _mark_file(root: Path) -> Path:
    return root / "cs_intro_c" / "example-task" / "marks" / "s001.yaml"


# --------------------------------------------------------------------------
# 待ち行列
# --------------------------------------------------------------------------


def test_the_queue_lists_unreviewed_submissions(workspace: Path) -> None:
    response = _client(workspace).get("/")
    assert response.status_code == 200
    assert "s001.c" in response.text
    assert "未採点" in response.text


def test_the_queue_shows_progress_towards_being_measurable(workspace: Path) -> None:
    """あと何件で κ が測れるようになるかを出す。作業の意味が見えるように。"""
    body = _client(workspace).get("/").text
    assert "0 / 30 件" in body
    assert "あと 30 件" in body


# --------------------------------------------------------------------------
# blind 採点 — ここが設計の中心
# --------------------------------------------------------------------------


def test_the_blind_page_contains_no_trace_of_the_ai_verdict(workspace: Path) -> None:
    """AI の判定が本文のどこにも無いこと。

    CSS で隠すのでは不十分（ソースを見れば分かる）。そもそも
    この時点では採点を走らせていない。
    """
    client = _client(workspace)
    body = client.get(f"/review/{ENTRY}/blind").text

    # 判定の中身に触れるものが 1 つも無いこと。
    # （説明文に「AI の判定は後で出ます」と書いてあるのは中身ではない。）
    for leak in (
        "AIRATIONALEMARKER",  # AI の rationale
        "確信度",  # 確信度の表示
        "不一致",  # 突き合わせの結果
        "ln hl",  # 根拠行のハイライト
        "最終確定",  # 開示後のフォーム
        "L5–5",  # 根拠の行範囲
    ):
        assert leak not in body, f"blind 画面に {leak!r} が漏れている"

    # 提出コードと観点は出ている。
    assert "int b = 0, c = 0, d = 0;" in body
    assert "変数名と構造の分かりやすさ" in body


def test_the_blind_page_does_not_grade_yet(workspace: Path) -> None:
    """採点して隠すのではなく、走らせない。LLM も呼ばれない。"""
    registry = EvaluatorRegistry().load_installed()
    provider = ScriptedProvider([AI_SAYS_3] * PROFILE_SAMPLES)
    registry.replace(RubricAiJudge(LlmGateway(provider), model="stub"))
    console = Console(ReviewStore(workspace), PROFILES, registry=registry, marker="tester")

    TestClient(create_app(console)).get(f"/review/{ENTRY}/blind")

    assert provider.calls == []
    assert not (workspace / "cs_intro_c" / "example-task" / "runs").exists()


def test_reveal_redirects_back_to_blind_when_not_yet_marked(workspace: Path) -> None:
    """blind 採点を飛ばして AI の判定を見ることはできない。"""
    client = _client(workspace)
    response = client.get(f"/review/{ENTRY}/reveal", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/blind")


# --------------------------------------------------------------------------
# 採点の保存
# --------------------------------------------------------------------------


@needs_c_compiler
def test_marking_writes_a_blind_golden_entry(workspace: Path) -> None:
    client = _client(workspace)
    response = client.post(
        f"/review/{ENTRY}/blind",
        data={"levels": ["correctness=3", "readability=1"], "notes": "変数名が読めない"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/reveal")

    saved = yaml.safe_load(_mark_file(workspace).read_text(encoding="utf-8"))
    assert saved["marks"] == {"correctness": 3, "readability": 1}
    assert saved["marker"] == "tester"
    assert saved["blind"] is True
    assert saved["notes"] == "変数名が読めない"


@needs_c_compiler
def test_marking_triggers_grading(workspace: Path) -> None:
    client = _client(workspace)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})
    runs = workspace / "cs_intro_c" / "example-task" / "runs"
    assert (runs / "s001.json").is_file()


def test_a_missing_criterion_is_rejected(workspace: Path) -> None:
    """観点を取りこぼした採点は保存しない。欠けたまま κ を測ることになる。"""
    response = _client(workspace).post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3"]})
    assert response.status_code == 400
    assert "readability" in response.json()["detail"]
    assert not _mark_file(workspace).exists()


def test_a_level_outside_the_rubric_is_rejected(workspace: Path) -> None:
    response = _client(workspace).post(
        f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=9"]}
    )
    assert response.status_code == 400
    assert "readability" in response.json()["detail"]


# --------------------------------------------------------------------------
# 開示
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_reveal_page_shows_the_disagreement_and_the_evidence(workspace: Path) -> None:
    client = _client(workspace)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})
    body = client.get(f"/review/{ENTRY}/reveal").text

    assert "AIRATIONALEMARKER" in body
    assert "不一致" in body  # 教員 1 対 AI 3
    assert "一致" in body  # correctness は一致
    assert "L5–5" in body  # 根拠の行範囲
    assert 'class="ln hl"' in body  # その行がコード上で目立つ


@needs_c_compiler
def test_changing_the_grade_after_seeing_the_ai_does_not_touch_the_golden_entry(
    workspace: Path,
) -> None:
    """開示後に段階を変えても、測定に使うのは blind の側（ADR 0005）。"""
    client = _client(workspace)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})
    client.post(
        f"/review/{ENTRY}/finalize",
        data={"levels": ["correctness=3", "readability=3"], "comment": "AI の指摘で考えを変えた"},
    )

    golden = yaml.safe_load(_mark_file(workspace).read_text(encoding="utf-8"))
    assert golden["marks"]["readability"] == 1, "ゴールデンセットが上書きされている"
    assert golden["blind"] is True

    decision = workspace / "cs_intro_c" / "example-task" / "reviews" / "s001.json"
    saved = decision.read_text(encoding="utf-8")
    assert '"readability": 3' in saved
    assert '"changed_after_seeing_ai": true' in saved


@needs_c_compiler
def test_agreeing_with_the_ai_is_recorded_as_agreement(workspace: Path) -> None:
    """κ の材料になるので、同意したことも明示的に記録する。"""
    client = _client(workspace)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=3"]})
    client.post(f"/review/{ENTRY}/finalize", data={"levels": ["correctness=3", "readability=3"]})
    saved = (workspace / "cs_intro_c" / "example-task" / "reviews" / "s001.json").read_text(
        encoding="utf-8"
    )
    assert '"changed_after_seeing_ai": false' in saved


@needs_c_compiler
def test_regrading_never_overwrites_a_previous_run(workspace: Path) -> None:
    """GradingRun は不変（設計原則 P8）。ファイル保存でも規則は同じ。"""
    from aijudge_reviewconsole.store import ReviewStore as Store

    client = _client(workspace, [AI_SAYS_3, AI_SAYS_3])
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})

    store = Store(workspace)
    entry = store.find(ENTRY)
    assert entry is not None
    first = store.load_run(entry)
    assert first is not None

    store.save_run(entry, first.model_copy(update={"score_ratio": 0.5}))
    runs = sorted((workspace / "cs_intro_c" / "example-task" / "runs").iterdir())
    assert len(runs) == 2, "上書きされている"


def test_an_unknown_submission_is_404(workspace: Path) -> None:
    assert _client(workspace).get("/review/nope/nope/nope.c/blind").status_code == 404
