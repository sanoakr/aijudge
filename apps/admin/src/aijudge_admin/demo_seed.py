"""デモコースの定義を読んで投入する（#194）。

**リセットが戻すのはこのファイルの中身である。** `demo reset` はコースを
消して作り直し、ここから問題セットを入れ直す ── だから「当初の状態」が
コードではなく `subjects/demo/course.yaml` に書いてある必要がある。

**3 種類を 1 コースに置ける理由**は #195（採点のプロファイルを課題から取る）。
それ以前は画像・C・レポートを 3 つのコースに割るしかなかった。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aijudge_core import Course
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

from .course_definition import apply_course_definition
from .operations import AdminError

__all__ = ["DemoSeed", "demo_definition_path", "seed_demo_course"]

#: 定義の置き場所。**科目プロファイルと同じ木の下**に置く ── 運用では
#: `AIJUDGE_PROFILES_DIR` がリポジトリの外を指すので、そこに一緒に来る。
DEMO_DIR = "demo"
DEMO_FILE = "course.yaml"


def demo_definition_path(profiles_dir: Path) -> Path:
    return profiles_dir / DEMO_DIR / DEMO_FILE


@dataclass(frozen=True)
class DemoSeed:
    course: Course
    tasks: int
    created: bool


def seed_demo_course(
    database: Database,
    *,
    tenant_id: TenantId,
    profiles_dir: Path,
    authored_by: UserId,
) -> DemoSeed:
    """定義を読んでコースと課題を作る。**何度走らせても増えない。**

    読んで投入するのは `course_definition`（どのコースも同じ経路・
    `course apply`）で、デモに固有なのは定義の置き場所と KC の骨格だけ。
    """
    path = demo_definition_path(profiles_dir)
    if not path.exists():
        raise AdminError(f"デモコースの定義がありません: {path}")

    # **KC の骨格を先に入れる**（#194）。課題は登録済みの KC しか名指しできず
    # （`kc.assert_registered`）、デモの KC は `demo` 名前空間にしかない ──
    # 「先に `kc seed` を叩いてください」と案内する形にすると、リセットの
    # たびに 2 つ叩くことになり、片方を忘れた日に課題が入らない。
    #
    # 骨格の投入も冪等なので、毎回通してよい。
    _seed_skeleton(database, profiles_dir)

    applied = apply_course_definition(
        database, path, tenant_id=tenant_id, profiles_dir=profiles_dir, authored_by=authored_by
    )
    return DemoSeed(course=applied.course, tasks=applied.tasks, created=applied.created)


def _seed_skeleton(database: Database, profiles_dir: Path) -> None:
    """`demo` 名前空間の骨格を投入する。**無ければ何もしない。**

    骨格が無いのは設定の不足であって、ここで落とす種類の失敗ではない ──
    課題の投入がそのあと「登録されていない知識要素です」で止まるので、
    どこで足りないかはそちらが言う。
    """
    from .kc import seed as seed_kcs
    from .kc_skeleton import load_skeleton, skeleton_dir

    path = skeleton_dir(profiles_dir) / "demo.yaml"
    if not path.exists():
        return
    skeleton = load_skeleton(path)
    seed_kcs(database, skeleton, namespaces=(skeleton.namespace,))
