"""レビューコンソールの規則を固定する。

固定したい規則は 2 つ。

1. **コンソールは採点しない。** 採点はワーカーが先に走らせる。レビューが
   採点を起動すると、測定用データの入力が採点の前提条件に戻る（ADR 0007）。
2. **blind 画面に AI の判定が漏れていない。** CSS で隠すのでは不十分なので、
   レスポンス本文を直接検査している。
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
from aijudge_reviewconsole import (
    Console,
    Grader,
    ReviewStore,
    TaskLoader,
    create_app,
    grade_pending,
    is_blind_sample,
)

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
TASK_DIR = ("cs_intro_c", "example-task")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """例のデータを複製し、教員採点を消して未レビューにする。"""
    root = tmp_path / "golden"
    shutil.copytree(EXAMPLE_GOLDEN, root)
    (root.joinpath(*TASK_DIR) / "marks" / "s001.yaml").unlink()
    return root


def _registry(responses: list[str] | None = None) -> tuple[EvaluatorRegistry, ScriptedProvider]:
    scripted = [r for r in (responses or [AI_SAYS_3]) for _ in range(PROFILE_SAMPLES)]
    provider = ScriptedProvider(scripted)
    registry = EvaluatorRegistry().load_installed()
    registry.replace(RubricAiJudge(LlmGateway(provider), model="stub"))
    return registry, provider


def _console(root: Path, *, blind_all: bool = False) -> Console:
    """コンソールを作る。抽出率は既定では科目プロファイルの宣言に従う。"""
    console = Console(ReviewStore(root), PROFILES, marker="tester")
    if blind_all:
        # 抽出に当たったかどうかに依存しないテストのために、全件を対象にする。
        console._rates["cs_intro_c"] = 1.0
    else:
        console._rates["cs_intro_c"] = 0.0
    return console


def _client(root: Path, *, blind_all: bool = False) -> TestClient:
    return TestClient(create_app(_console(root, blind_all=blind_all)))


def _grade(root: Path, responses: list[str] | None = None) -> None:
    """ワーカーで採点する。レビューの前に済んでいる状態を作る。"""
    registry, _ = _registry(responses)
    store = ReviewStore(root)
    grade_pending(Grader(store, PROFILES, TaskLoader(), registry=registry))


def _mark_file(root: Path) -> Path:
    return root.joinpath(*TASK_DIR) / "marks" / "s001.yaml"


def _runs_dir(root: Path) -> Path:
    return root.joinpath(*TASK_DIR) / "runs"


def _observations_dir(root: Path) -> Path:
    return root.joinpath(*TASK_DIR) / "observations"


# --------------------------------------------------------------------------
# 採点はレビューの前提条件ではない — ADR 0007 の中心
# --------------------------------------------------------------------------


def test_the_console_never_grades(workspace: Path) -> None:
    """コンソールを一巡させても採点は走らない。LLM も呼ばれない。

    以前は blind 採点の保存が採点を起動していた。順序が逆だった。
    """
    registry, provider = _registry()
    console = Console(ReviewStore(workspace), PROFILES, registry=registry, marker="tester")
    console._rates["cs_intro_c"] = 1.0
    client = TestClient(create_app(console))

    client.get("/")
    client.get(f"/review/{ENTRY}/blind")
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})

    assert provider.calls == [], "コンソールが LLM を呼んでいる"
    assert not _runs_dir(workspace).exists(), "コンソールが採点している"


def test_reveal_says_the_grading_has_not_arrived_yet(workspace: Path) -> None:
    """採点が届いていない提出は 409。ここで採点し始めない。

    AI 評価は非同期で「あとから届く」のが前提なので、未着は異常ではない。
    """
    response = _client(workspace).get(f"/review/{ENTRY}/reveal")
    assert response.status_code == 409
    assert "aijudge-grade" in response.json()["detail"]


def test_the_queue_shows_what_is_still_waiting_to_be_graded(workspace: Path) -> None:
    body = _client(workspace).get("/").text
    assert "採点待ち" in body
    assert "aijudge-grade" in body


@needs_c_compiler
def test_the_worker_grades_without_any_instructor_marking(workspace: Path) -> None:
    """教員が何もしていない状態で採点が完了すること。"""
    _grade(workspace)
    assert (_runs_dir(workspace) / "s001.json").is_file()
    assert not _mark_file(workspace).exists(), "採点が教員採点を要求している"


@needs_c_compiler
def test_grading_is_idempotent(workspace: Path) -> None:
    """既に採点済みの提出は引き直さない（採点結果は不変・P8）。"""
    _grade(workspace)
    before = sorted(p.name for p in _runs_dir(workspace).iterdir())
    _grade(workspace)
    assert sorted(p.name for p in _runs_dir(workspace).iterdir()) == before


# --------------------------------------------------------------------------
# blind 抽出
# --------------------------------------------------------------------------


def test_sampling_is_deterministic() -> None:
    """同じ提出は毎回同じ判定。実行ごとに変わると測定が再現しない。"""
    first = [is_blind_sample(f"s{i:03d}.c", 0.3) for i in range(200)]
    second = [is_blind_sample(f"s{i:03d}.c", 0.3) for i in range(200)]
    assert first == second


def test_sampling_hits_roughly_the_declared_rate() -> None:
    """宣言した比率に概ね一致すること。偏ると標本が貯まらない。"""
    hits = sum(1 for i in range(2000) if is_blind_sample(f"s{i:04d}.c", 0.25))
    assert 0.20 < hits / 2000 < 0.30


def test_a_rate_of_zero_never_asks_for_blind_marking() -> None:
    """測定用データを集めない設定。採点は変わらず動く。"""
    assert not any(is_blind_sample(f"s{i:03d}.c", 0.0) for i in range(100))


@needs_c_compiler
def test_a_submission_outside_the_sample_skips_blind_marking(workspace: Path) -> None:
    """抽出対象外の提出に blind 採点を求めない。"""
    _grade(workspace)
    response = _client(workspace).get(f"/review/{ENTRY}/blind", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/reveal")


@needs_c_compiler
def test_a_sampled_submission_must_be_marked_before_the_verdict_is_shown(
    workspace: Path,
) -> None:
    """抽出対象は blind 採点を飛ばして AI の判定を見られない。"""
    _grade(workspace)
    client = _client(workspace, blind_all=True)
    response = client.get(f"/review/{ENTRY}/reveal", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/blind")


# --------------------------------------------------------------------------
# blind 画面 — AI の判定が漏れていないこと
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_blind_page_contains_no_trace_of_the_ai_verdict(workspace: Path) -> None:
    """採点済みでも、blind 画面には判定を載せない。

    以前は「まだ採点していないから漏れない」という理屈だったが、いまは
    採点が先に済んでいる。**隠すのではなくレスポンスに含めない**ことを
    改めて固定する。
    """
    _grade(workspace)
    body = _client(workspace, blind_all=True).get(f"/review/{ENTRY}/blind").text

    for leak in (
        "AIRATIONALEMARKER",  # AI の rationale
        "確信度",  # 確信度の表示
        "不一致",  # 突き合わせの結果
        "ln hl",  # 根拠行のハイライト
        "最終確定",  # 開示後のフォーム
        "L5–5",  # 根拠の行範囲
    ):
        assert leak not in body, f"blind 画面に {leak!r} が漏れている"

    assert "int b = 0, c = 0, d = 0;" in body
    assert "変数名と構造の分かりやすさ" in body


# --------------------------------------------------------------------------
# 採点の保存
# --------------------------------------------------------------------------


@needs_c_compiler
def test_marking_writes_a_blind_mark(workspace: Path) -> None:
    _grade(workspace)
    client = _client(workspace, blind_all=True)
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


def test_a_missing_criterion_is_rejected(workspace: Path) -> None:
    """観点を取りこぼした採点は保存しない。欠けたまま κ を測ることになる。"""
    response = _client(workspace, blind_all=True).post(
        f"/review/{ENTRY}/blind", data={"levels": ["correctness=3"]}
    )
    assert response.status_code == 400
    assert "readability" in response.json()["detail"]
    assert not _mark_file(workspace).exists()


def test_a_level_outside_the_rubric_is_rejected(workspace: Path) -> None:
    response = _client(workspace, blind_all=True).post(
        f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=9"]}
    )
    assert response.status_code == 400
    assert "readability" in response.json()["detail"]


# --------------------------------------------------------------------------
# 開示と確定
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_reveal_page_shows_the_disagreement_and_the_evidence(workspace: Path) -> None:
    _grade(workspace)
    client = _client(workspace, blind_all=True)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})
    body = client.get(f"/review/{ENTRY}/reveal").text

    assert "AIRATIONALEMARKER" in body
    assert "不一致" in body  # 教員 1 対 AI 3
    assert "一致" in body  # correctness は一致
    assert "L5–5" in body  # 根拠の行範囲
    assert 'class="ln hl"' in body  # その行がコード上で目立つ


@needs_c_compiler
def test_an_unsampled_submission_can_be_finalized_without_any_blind_mark(
    workspace: Path,
) -> None:
    """抽出対象外の提出は、AI の判定を見てそのまま確定できる。

    これが通常の経路で、大多数の提出がこれに当たる。
    """
    _grade(workspace)
    client = _client(workspace)
    assert client.get(f"/review/{ENTRY}/reveal").status_code == 200

    client.post(f"/review/{ENTRY}/finalize", data={"levels": ["correctness=3", "readability=3"]})
    decision = (workspace.joinpath(*TASK_DIR) / "reviews" / "s001.json").read_text(encoding="utf-8")
    assert '"blind_levels": {}' in decision
    assert '"changed_after_seeing_ai": false' in decision
    assert not _mark_file(workspace).exists()


@needs_c_compiler
def test_overriding_the_ai_without_a_blind_mark_is_recorded_as_a_change(
    workspace: Path,
) -> None:
    """blind 採点が無い提出では、AI の判定が「変えたか」の基準になる。

    見逃し率は「AI をそのまま通したが実は直すべきだった」を測る指標なので、
    抽出対象外の提出でもこの記録が必要。
    """
    _grade(workspace)
    client = _client(workspace)
    client.post(f"/review/{ENTRY}/finalize", data={"levels": ["correctness=3", "readability=1"]})
    decision = (workspace.joinpath(*TASK_DIR) / "reviews" / "s001.json").read_text(encoding="utf-8")
    assert '"changed_after_seeing_ai": true' in decision


@needs_c_compiler
def test_changing_the_grade_after_seeing_the_ai_does_not_touch_the_blind_mark(
    workspace: Path,
) -> None:
    """開示後に段階を変えても、測定に使うのは blind の側（ADR 0005）。"""
    _grade(workspace)
    client = _client(workspace, blind_all=True)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})
    client.post(
        f"/review/{ENTRY}/finalize",
        data={"levels": ["correctness=3", "readability=3"], "comment": "AI の指摘で考えを変えた"},
    )

    mark = yaml.safe_load(_mark_file(workspace).read_text(encoding="utf-8"))
    assert mark["marks"]["readability"] == 1, "blind 採点が上書きされている"
    assert mark["blind"] is True

    decision = (workspace.joinpath(*TASK_DIR) / "reviews" / "s001.json").read_text(encoding="utf-8")
    assert '"readability": 3' in decision
    assert '"changed_after_seeing_ai": true' in decision


@needs_c_compiler
def test_agreeing_with_the_ai_is_recorded_as_agreement(workspace: Path) -> None:
    """κ の材料になるので、同意したことも明示的に記録する。"""
    _grade(workspace)
    client = _client(workspace, blind_all=True)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=3"]})
    client.post(f"/review/{ENTRY}/finalize", data={"levels": ["correctness=3", "readability=3"]})
    decision = (workspace.joinpath(*TASK_DIR) / "reviews" / "s001.json").read_text(encoding="utf-8")
    assert '"changed_after_seeing_ai": false' in decision


@needs_c_compiler
def test_finalizing_never_overwrites_a_previous_run(workspace: Path) -> None:
    """GradingRun は不変（設計原則 P8）。ファイル保存でも規則は同じ。"""
    _grade(workspace)
    store = ReviewStore(workspace)
    entry = store.find(ENTRY)
    assert entry is not None
    first = store.load_run(entry)
    assert first is not None

    store.save_run(entry, first.model_copy(update={"score_ratio": 0.5}))
    assert len(list(_runs_dir(workspace).iterdir())) == 2, "上書きされている"


def test_an_unknown_submission_is_404(workspace: Path) -> None:
    assert _client(workspace).get("/review/nope/nope/nope.c/blind").status_code == 404


# --------------------------------------------------------------------------
# 観測レコード — 測定への唯一の受け渡し
# --------------------------------------------------------------------------


@needs_c_compiler
def test_grading_writes_observations(workspace: Path) -> None:
    """採点の副産物として観測が書かれる。測定用に別途集める作業は無い。"""
    _grade(workspace)
    store = ReviewStore(workspace)
    entry = store.find(ENTRY)
    assert entry is not None

    observations = store.load_observations(entry)
    codes = {item.criterion_code for item in observations}
    assert codes == {"correctness", "readability"}

    readability = next(item for item in observations if item.criterion_code == "readability")
    assert readability.machine_level == 3
    assert readability.human_level is None, "教員採点が無いのに入っている"
    assert readability.blind is False
    assert readability.levels == (0, 1, 2, 3)
    assert not readability.usable_for_agreement, "教員採点なしで標本に入っている"


@needs_c_compiler
def test_a_blind_mark_becomes_a_usable_observation(workspace: Path) -> None:
    _grade(workspace)
    client = _client(workspace, blind_all=True)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})
    client.post(f"/review/{ENTRY}/finalize", data={"levels": ["correctness=3", "readability=1"]})

    store = ReviewStore(workspace)
    entry = store.find(ENTRY)
    assert entry is not None
    readability = next(
        item for item in store.load_observations(entry) if item.criterion_code == "readability"
    )
    assert readability.human_level == 1
    assert readability.machine_level == 3
    assert readability.blind is True
    assert readability.changed_after_seeing_ai is False
    assert readability.usable_for_agreement


@needs_c_compiler
def test_the_deterministic_criterion_is_excluded_from_agreement(workspace: Path) -> None:
    """テスト実行で確定した観点は AI の精度ではない。標本から外す。"""
    _grade(workspace)
    client = _client(workspace, blind_all=True)
    client.post(f"/review/{ENTRY}/blind", data={"levels": ["correctness=3", "readability=1"]})

    store = ReviewStore(workspace)
    entry = store.find(ENTRY)
    assert entry is not None
    correctness = next(
        item for item in store.load_observations(entry) if item.criterion_code == "correctness"
    )
    assert correctness.conclusive
    assert not correctness.usable_for_agreement


@needs_c_compiler
def test_a_broken_observation_write_does_not_fail_the_grading(workspace: Path) -> None:
    """観測の書き出しが失敗しても採点は成立する（P2 / ADR 0007）。"""
    registry, _ = _registry()
    store = ReviewStore(workspace)
    grader = Grader(store, PROFILES, TaskLoader(), registry=registry)

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("disk on fire")

    store.save_observations = explode  # type: ignore[method-assign]
    entry = store.queue()[0]
    run = grader.grade(entry)

    assert run.score_ratio >= 0.0
    assert (_runs_dir(workspace) / "s001.json").is_file()
    assert not _observations_dir(workspace).exists()
