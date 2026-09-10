"""デモコースの定義から作る（#194 の C）。

**定義はファイルにある**（`subjects/demo/course.yaml`）。コードに書かない
理由は、リセットがそこから戻すからである ── 「当初の状態」がコードだと、
戻すたびにコードを読み直すことになる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin.demo_seed import demo_definition_path, seed_demo_course
from aijudge_admin.operations import AdminError
from aijudge_core import HUMAN_SCORED
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
AUTHOR = UserId("usr_" + "a" * 32)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/seed.db", create=True)
    yield db
    db.dispose()


def _seed(database: Database):
    return seed_demo_course(database, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR)


def test_the_definition_ships_with_the_profiles() -> None:
    """定義は科目プロファイルと同じ木の下にある。

    運用では `AIJUDGE_PROFILES_DIR` がリポジトリの外を指すので、そこに
    一緒に来る必要がある（`subjects/README.md`）。
    """
    assert demo_definition_path(PROFILES).exists()


def test_three_kinds_of_task_live_in_one_course(database: Database) -> None:
    """**これが要件の核心**（#194 の 4）。

    画像・プログラム・レポートが 1 つのコースに同居する。できるように
    なったのは #195（採点のプロファイルを課題から取る）以降で、それ以前は
    3 つのコースに割るしかなかった。
    """
    result = _seed(database)
    assert result.tasks == 3

    with database.unit_of_work() as uow:
        versions = {
            task.title: uow.tasks.latest_version(task.id)
            for task in uow.tasks.list_for_course(result.course.id)
        }
    profiles = {title: version.subject_profile for title, version in versions.items()}
    assert set(profiles.values()) == {"demo_image", "cs_lang_c_intro", "report_ja"}


def test_the_image_task_has_no_machine_evaluator(database: Database) -> None:
    """画像の課題は**人が採点する観点しか持たない**。

    その結果、提出しても**総合点は保留になる**（`visibility.py`）── 残った
    観点だけを比例配分した合計を出さない、という設計の実物である。
    **その状態がデモで見えること自体が説明になる。**
    """
    result = _seed(database)
    with database.unit_of_work() as uow:
        version = next(
            uow.tasks.latest_version(task.id)
            for task in uow.tasks.list_for_course(result.course.id)
            if uow.tasks.latest_version(task.id).subject_profile == "demo_image"
        )
    assert version.criteria
    assert all(c.evaluator_id == HUMAN_SCORED for c in version.criteria), (
        "機械が採点する観点が混ざっている。総合点が出てしまう"
    )


def test_the_demo_uses_its_own_knowledge_components(database: Database) -> None:
    """**`cs` を使わない**（#194 の分離の設計）。

    デモの提出は数えないが、その除外がどこか 1 か所でも漏れたときに
    本物の習熟度へ効かないよう、名前空間ごと分けておく ── 漏れても
    積み上がるのは `demo.*` で、それを読む運用コースは存在しない。
    """
    import yaml

    data = yaml.safe_load(demo_definition_path(PROFILES).read_text(encoding="utf-8"))
    keys = [kc for task in data["tasks"] for kc in task.get("knowledge_components", [])]
    assert keys, "KC を宣言していない"
    assert all(key.startswith("demo.") for key in keys), (
        f"デモ以外の名前空間を使っている: {[k for k in keys if not k.startswith('demo.')]}"
    )


def test_seeding_twice_adds_nothing(database: Database) -> None:
    """**何度走らせても増えない**（`kc seed` と同じ作法）。

    リセットが毎回これを通るので、冪等でないと課題が倍々に増える。
    """
    first = _seed(database)
    second = _seed(database)

    assert second.course.id == first.course.id
    with database.unit_of_work() as uow:
        assert len(uow.tasks.list_for_course(first.course.id)) == 3


def test_a_missing_definition_says_so(database: Database, tmp_path: Path) -> None:
    """定義が無ければ、そう言って止まる。**空のコースを作らない。**"""
    with pytest.raises(AdminError, match="定義がありません"):
        seed_demo_course(database, tenant_id=TENANT, profiles_dir=tmp_path, authored_by=AUTHOR)
