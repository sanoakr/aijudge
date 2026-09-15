"""生成課題のレビュー画面の規則を固定する（S2、設計方針 §5）。

固定したいのは 5 つ。

TA には開けない    課題を出題可能にする操作は教員の権限（`_require_instructor`）。
他コースを探れない  存在と権限を区別しない。
理由なく却下できない 作問改善の材料であり、承認率の分母でもある。
知識要素を必ず出す  機械が検証していないので、見せなければ誰も気づかない。
二度は決められない  やり直しは新しい版から（P8）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_authoring import DraftKind, TaskDraftRecord, TaskSpec
from aijudge_authoring.spec import TestCaseSpec
from aijudge_authoring.verification import GateOutcome, TaskChecks, VerificationReport
from aijudge_core import (
    Course,
    KnowledgeComponent,
    Role,
    kc_id_for,
)
from aijudge_core.ids import (
    CourseId,
    TenantId,
)
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
OTHER_COURSE = CourseId("crs_" + "9" * 32)
DRAFT = "dft_" + "3" * 32
PASSWORD = "correct horse battery"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.console = Console(self.database, self.store, profiles_dir=PROFILES)
        self.client = TestClient(create_app(self.console))

        with self.database.unit_of_work() as uow:
            for cid, code in ((COURSE, "prog2"), (OTHER_COURSE, "other")):
                uow.identity.save_course(
                    Course(
                        id=cid,
                        tenant_id=TENANT,
                        code=code,
                        title=f"演習 {code}",
                        term="2026-前期",
                        subject_profile="cs_lang_c_intro",
                        # 作問の候補はコースに足したものだけ（#289）。
                        knowledge_components=("cs.loops.termination",),
                    )
                )
            # **知識要素そのものを登録する。** 画面は Q-matrix から引いて
            # キーを出すので（#267）、登録が無いと ID のまま出る。検査記録の
            # `declared_kcs` は「門が何を宣言として見たか」の記録であって、
            # この課題が何を問うかの出どころではない。
            uow.skills.save_kc(
                KnowledgeComponent(
                    # **ID は正準キーから導く。** 課題が KC を名指しできるか
                    # の検査は `kc_id_for(key)` で引くので、別の ID で入れると
                    # 「登録されていない」と言われる（#321 で採用の経路が
                    # `save_task` を通るようになって表に出た）。
                    id=kc_id_for("cs.loops.termination"),
                    namespace="cs",
                    path=("loops", "termination"),
                    label="ループの停止条件",
                )
            )
            # **課題は作らない**（#321）。承認待ちは下書きで、採用したときに
            # 初めて課題になる ── それまで同一性（課題キー → 課題 ID）は
            # 決まっていない。
            uow.tasks.save_draft(_draft())
            uow.commit()

    def register(self, login: str, role: Role, course: CourseId = COURSE):
        with self.database.unit_of_work() as uow:
            service = AuthService(uow.identity, audit=uow.audit)
            principal = service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
            service.enroll(tenant_id=TENANT, course_id=course, user_id=principal.user_id, role=role)
            uow.commit()
        return principal

    def login(self, login: str) -> None:
        response = self.client.post(
            "/auth/local", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        self.client.cookies.set(SESSION_COOKIE, response.cookies[SESSION_COOKIE])


def _draft() -> TaskDraftRecord:
    return TaskDraftRecord(
        id=DRAFT,
        course_id=COURSE,
        kind=DraftKind.NEW,
        spec=TaskSpec(
            key="ex07/generated",
            title="生成課題",
            statement="## 生成された課題 ##\n\n2 つの整数を読み、和を出力しなさい。",
            knowledge_components=("cs.loops.termination",),
            test_cases=(
                TestCaseSpec(name="case1", input="1 2\n", expected="3\n"),
                TestCaseSpec(name="case2", input="2 2\n", expected="4\n"),
            ),
            reference_solution="int main(void){return 0;}\n",
        ),
        generated_by="stub",
        generation_prompt_version="task_draft_ja@1",
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        subject_profile="cs_lang_c_intro",
        checks=TaskChecks(
            verification=VerificationReport(
                reference_passes=GateOutcome.PASSED,
                mutants_total=5,
                mutants_killed=5,
            ),
            declared_kcs=("cs.loops.termination",),
            checked_at=datetime(2026, 8, 29, tzinfo=UTC),
        ),
    )


@pytest.fixture
def world(tmp_path: Path):
    made = World(tmp_path)
    yield made
    made.database.dispose()


def _url(course: CourseId = COURSE) -> str:
    return f"/manage/courses/{course}/drafts"


def test_an_assistant_cannot_open_the_queue(world: World) -> None:
    """**TA には開けない。** 課題を出題可能にするのは教員の権限。"""
    world.register("ta1", Role.ASSISTANT)
    world.login("ta1")
    assert world.client.get(_url()).status_code == 403


def test_a_stranger_cannot_tell_the_course_exists(world: World) -> None:
    """存在と権限を区別しない（他コースを列挙させない）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    assert world.client.get(_url(OTHER_COURSE)).status_code == 404


def test_the_queue_always_shows_the_knowledge_components(world: World) -> None:
    """**機械が検証していないので、見せなければ誰も気づかない。**"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    body = world.client.get(_url()).text

    assert "cs.loops.termination" in body
    assert "課題文がこれを問うているか確認してください" in body
    assert "機械はここを検証していません" in body


def test_the_components_are_shown_even_without_a_checks_record(world: World) -> None:
    """**検査記録の有無と、問う知識要素があるかは別の事実**（#267）。

    以前は検査記録（`TaskChecks.declared_kcs`）から出していたので、記録が
    無いだけで「登録なし（この課題では習熟度が付きません）」と出た ──
    Q-matrix には入っており、習熟度は付く。**教員が承認を判断する瞬間に
    事実でないことを伝えていた**（P5）。

    本番で踏んだ。AI 作問は門の検査を記録していなかったので、**この画面が
    確認するためにある対象そのもの**が、常にこの嘘を出していた。
    """
    # 検査の記録が無い下書き（検査が動かない環境・サンドボックス不在で起きる）。
    with world.database.unit_of_work() as uow:
        uow.tasks.save_draft(_draft().model_copy(update={"checks": None}))
        uow.commit()

    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    body = world.client.get(_url()).text

    assert "cs.loops.termination" in body, "検査記録が無いと知識要素が消える"
    assert "登録なし（この課題では習熟度が付きません）" not in body


def test_the_queue_says_approval_is_what_publishes(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    assert "承認するまで出題されません" in world.client.get(_url()).text


def test_the_queue_lets_you_fix_the_draft_before_taking_it(world: World) -> None:
    """**採用の瞬間まで何でも直せる**（#321）。

    課題はまだ無いので、直しても版は増えない ── 以前は生成の時点で課題に
    なっていたので、直すには「版を上げる」しかなく、キーに至っては**二度と
    変えられなかった**。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    body = world.client.get(_url()).text
    assert 'name="key_suffix"' in body, "採用時にキーを直せない"
    assert 'name="statement"' in body
    assert 'name="title"' in body


def test_taking_a_draft_creates_the_task(world: World) -> None:
    """**採用したときに初めて課題になる**（#321）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    response = world.client.post(
        f"{_url()}/{DRAFT}",
        data={"decision": "approve", "key_suffix": "generated", "unit": "ex07"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(COURSE)
        assert len(tasks) == 1, "採用したのに課題ができていない"
        version = uow.tasks.latest_published_version(tasks[0].id)
        assert version is not None, "採用した課題が出題されない"
        # 出所は残る（P8）。承認したのは人だが、書いたのはモデルである。
        assert version.provenance.generated_by == "stub"
        # 門の記録は課題版へ移る（下書きは消えるので、ここに無いと失われる）。
        assert uow.tasks.get_checks(version.id) is not None
        # 下書きはもう無い。
        assert uow.tasks.list_drafts(COURSE) == ()


def test_the_key_is_settled_when_the_draft_is_taken(world: World) -> None:
    """**キーは採用のときに決まる**（#321）。

    キーは変えられない同一性の鍵で、提出も採点もここにぶら下がる ── 生成の
    時点で決め切ると、中身を読む前に名前が確定してしまう。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    world.client.post(
        f"{_url()}/{DRAFT}",
        data={"decision": "approve", "key_suffix": "sum-two", "unit": "ex07"},
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(COURSE)[0]
        version = uow.tasks.latest_version(task.id)
    assert version.source_key == "ex07/sum-two", version.source_key


def test_dropping_a_draft_removes_it_and_creates_nothing(world: World) -> None:
    """**捨てるのは即時の削除**（ADR 0019）。理由は聞かない。

    却下の記録を残さないと決めたので（承認率は測らない）、書かせる意味が無い。
    捨てても学習者には何も影響しない ── 課題になっていないのだから。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    response = world.client.post(
        f"{_url()}/{DRAFT}", data={"decision": "drop"}, follow_redirects=False
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        assert uow.tasks.list_drafts(COURSE) == ()
        assert uow.tasks.list_for_course(COURSE) == (), "捨てたのに課題ができている"


def test_a_draft_decided_twice_is_gone_the_second_time(world: World) -> None:
    """二度押しは普通に起きる。**1 度目で消えている**ので 404。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    world.client.post(f"{_url()}/{DRAFT}", data={"decision": "drop"})

    again = world.client.post(
        f"{_url()}/{DRAFT}", data={"decision": "drop"}, follow_redirects=False
    )
    assert again.status_code == 404


def test_the_queue_can_generate_without_choosing_a_set(world: World) -> None:
    """**作問の時点では出題先を決めない**（#84）。

    使えるかどうかは作ってみないと分からないので、先に決めると、却下した
    ときにそのセットの一覧に残骸が並ぶ。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    client = world.client

    page = client.get(f"/manage/courses/{COURSE}/drafts").text
    # 出題先は承認のときに選ぶ。
    assert "出題する問題セット" in page
    assert 'name="unit"' in page
    # 問う知識要素は作問の時点で選ぶ（出題先とは別）。**知識要素が無ければ
    # 生成そのものを断る**という規則は `test_manage.py` が覆っており、この
    # コースには登録があるので、ここでは選ぶ欄が出ることを見る。
    assert "問う知識要素" in page


def test_taking_a_draft_places_the_task_in_the_chosen_set(world: World) -> None:
    """採用が「どこに出すか」を決める唯一の場面（#84）。

    日程は選んだセットに揃う ── 課題の移動と同じ規則（`_place_in_unit`）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")

    response = world.client.post(
        f"/manage/courses/{COURSE}/drafts/{DRAFT}",
        data={"decision": "approve", "key_suffix": "generated", "unit": "ex07"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(COURSE)[0]
    assert task.unit == "ex07", "採用で選んだセットに入っていない"


def test_the_queue_shows_the_statement_as_the_learner_sees_it(world: World) -> None:
    """承認は学生が読む画面を見て決める。Markdown の生文だけでは、数式や
    コードの囲みが意図どおりかが分からない（課題の編集画面と同じ描画）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    page = world.client.get(f"/manage/courses/{COURSE}/drafts").text
    assert "学習者に出る形" in page
    # 見出しは描画されて h2 になる。原文も折り畳んで残る。
    assert "<h2>生成された課題</h2>" in page
    assert "Markdown の原文" in page
    assert "## 生成された課題 ##" in page
