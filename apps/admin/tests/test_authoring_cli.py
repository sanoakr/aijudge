"""作問の口を固定する（S2、設計方針 §5・#522）。

固定したいのは 3 つ。

保存しても出題されない  生成物は下書き（`TaskDraftRecord`）になり、課題にはならない
                        （設計原則 P5・ADR 0019）。
承認は画面で           下書きはコンソールの「未承認の課題（AI 作問）」に並ぶ。
                        CLI に承認・却下の口は無い（規則を 2 か所に書かない）。
門が落ちても捨てない    捨てると、門が厳しすぎることに誰も気づけない。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import pytest

import aijudge_admin.authoring_cli as module
from aijudge_admin.authoring_cli import cmd_task_draft, register
from aijudge_authoring import GateOutcome, VerificationReport
from aijudge_authoring.drafting import DraftTestCase, TaskDraft, draft_to_spec
from aijudge_core import Course, KnowledgeComponent
from aijudge_core.ids import CourseId, KcId, TenantId, UserId
from aijudge_course_admin.drafting import DraftResult
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
INSTRUCTOR = UserId("usr_" + "2" * 32)
KC = KcId("kc_" + "4" * 32)


@pytest.fixture
def database(monkeypatch):
    made = Database.connect("sqlite+pysqlite:///:memory:", create=True)

    # CLI は自分で接続を開く。テストではインメモリ DB を共有させる。
    monkeypatch.setattr(module, "_open", lambda args: made)
    monkeypatch.setattr(made, "dispose", lambda: None)

    with made.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=COURSE,
                tenant_id=TENANT,
                code="prog2",
                title="演習",
                term="2026-前期",
                subject_profile="cs_lang_c_intro",
            )
        )
        uow.skills.save_kc(
            KnowledgeComponent(
                id=KC, namespace="cs", path=("loops", "termination"), label="ループの停止"
            )
        )
        uow.commit()
    yield made
    made.engine.dispose()


class _Drafter:
    """モデルを呼ばない作問役。"""

    def __init__(self, *a: object, **kw: object) -> None: ...

    def draft(self, blueprint, *, key):
        draft = TaskDraft(
            title="生成された課題",
            statement="## 生成 ##\n\n2 つの整数を読み、和を出力しなさい。",
            reference_solution="int main(void){return 0;}",
            test_cases=(
                DraftTestCase(name="case1", input="1 2", expected="3"),
                DraftTestCase(name="case2", input="2 3", expected="5"),
            ),
        )
        return DraftResult(
            spec=draft_to_spec(draft, blueprint, key=key),
            draft=draft,
            prompt_id="task_draft_ja@2",
            model="stub-model",
        )


class _FailingVerifier:
    """門 1 が落ちた、と言う検査役（サンドボックスを使わない）。"""

    def verify(self, version):
        return VerificationReport(
            reference_passes=GateOutcome.FAILED, reference_detail="参照解答が case1 を通らない"
        )


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "course": str(COURSE),
        "key": "gen/ex01",
        "author": str(INSTRUCTOR),
        "kc": ["cs.loops.termination"],
        "profile_name": "cs_lang_c_intro",
        "language": "c",
        "difficulty": "standard",
        "instruction": None,
        "constraint": None,
        "test_cases": 2,
        "model": None,
        "solver_model": None,
        "no_solvability": True,
        "embedding_model": None,
        "no_duplicates": True,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_a_generated_task_is_saved_as_a_draft_not_a_task(database, monkeypatch, capsys) -> None:
    """**下書きになる。** 画面の「未承認の課題」に並び、承認するまで課題は無い。

    門が落ちても捨てない（1 を返して知らせるが、保存はする）。
    """
    monkeypatch.setattr(module, "TaskDrafter", _Drafter)
    monkeypatch.setattr(module, "_verifier", lambda args, profile: _FailingVerifier())

    assert cmd_task_draft(_args()) == 1
    out = capsys.readouterr().out
    assert "下書きとして保存しました" in out
    assert "未承認の課題" in out

    with database.unit_of_work() as uow:
        drafts = uow.tasks.list_drafts(COURSE)
        tasks = uow.tasks.list_for_course(COURSE)
    assert tasks == (), "課題が作られている（承認するまで課題にしない）"
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.generated_by == "stub-model"
    assert draft.generation_prompt_version == "task_draft_ja@2"
    assert draft.created_by == INSTRUCTOR
    assert draft.unit == "", "出題先は承認のときに決める"
    assert draft.checks is not None
    assert draft.checks.verification.reference_passes is GateOutcome.FAILED


def test_a_dry_run_saves_nothing(database, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "TaskDrafter", _Drafter)
    monkeypatch.setattr(module, "_verifier", lambda args, profile: _FailingVerifier())

    cmd_task_draft(_args(dry_run=True))

    with database.unit_of_work() as uow:
        assert uow.tasks.list_drafts(COURSE) == ()


def test_the_cli_has_no_approval_of_its_own() -> None:
    """**承認・却下は画面だけ**（#522）。残るのは承認率だけ。"""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="task_command"))
    for gone in (["review", "list"], ["review", "decide", "--version", "x", "--reviewer", "y"]):
        with pytest.raises(SystemExit):
            parser.parse_args(gone)
    assert parser.parse_args(["review", "rate", "--course", str(COURSE)]).review_command == "rate"


def test_the_draft_time_is_recent(database, monkeypatch) -> None:
    """作った時刻を下書きに残す（一覧の並びと出所）。"""
    monkeypatch.setattr(module, "TaskDrafter", _Drafter)
    monkeypatch.setattr(module, "_verifier", lambda args, profile: _FailingVerifier())
    before = datetime.now(UTC)

    cmd_task_draft(_args())

    with database.unit_of_work() as uow:
        (draft,) = uow.tasks.list_drafts(COURSE)
    assert draft.created_at >= before
