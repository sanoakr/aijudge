"""コース・課題・受講の管理（教員向け）。

**YAML の直接編集を運用の前提にしない**（設計方針 §9.2 Phase 2）。学期の頭に
やることは CLI（`aijudge-admin`）でもできるが、学期中に発生する作業
── 締切の設定、受講者の追加、課題の追加 ── を教員がターミナルで行うのは
現実的でない。

課題の追加は**画面と API の両方から**でき、保存は同じ経路を通る
（`aijudge_course_admin.authoring.save_task`）。zip での一括取り込みは廃止した ── 移行元
（Sharif Judge）の形式をサーバの入口の語彙にしてしまっており、移行が
終わったあとも一生ついて回る形だった。まとまった投入は API で行う
（`api.py`）。

**コースが使っている科目プロファイル（`*.yaml`）は読み取り専用。** 1 つの
プロファイルは複数のコースの雛形になりうるので、書き換えると自分が担当して
いないコースの採点まで変わる（ADR 0002 の「コードと同じ扱いでレビューを
通す」はこの範囲のこと）。

そこで #146 で編集できる範囲を絞った ── **未参照のものだけ直接編集・改名
でき、参照中のものへの唯一の操作は「複製して編集」**（`/manage/subjects`）。
判定は `aijudge_course_admin.profiles` が持ち、この層は画面と繋ぐだけ。判定を画面に
写すと、食い違ったときに「画面では編集できるのに保存が拒否される」形で出る。
コース設定の画面（`_course_page`）から雛形を書く口は、以前どおり無い
── そこで触るのはコースごとの上書き（`aijudge_grading.overrides`）。

権限は 2 段。
- コースの作成・削除は **ADMIN**
- そのコースの課題・受講の管理は **INSTRUCTOR 以上**（TA には開けない。
  締切や受講の変更は成績に直接効く）

成績の確定もここに置く。教員の待ち行列は異議申立だけなので（ADR 0009）、
依頼が出なかった提出を閉じる導線がどこかに要る。課題ごとの一括確定と、
締切からの猶予（`auto_finalize_after_hours`）の設定がそれである
（ADR 0010）。**猶予は科目プロファイルではなくコースに持つ** ── 締切と
同じ性質の運用値で、教員が学期中に決めるものだから。

**画面の領域ごとのモジュールに分けてある**（段階的な立て直しの段階 4、
地図と経緯は `docs/design/manage-split-map.md`）。ここは `/manage` のルータを作り、
各領域の `register(router, templates)` を呼ぶだけ。

    users / subjects        テナント管理者の画面（利用者・学内ネットワーク・OIDC・
                            自分のパスワード）と科目プロファイル
    course                  コースの設定・作成・削除・複製
    units / groups          問題セット、受講者のグループと課題文の画像
    tasks / task_data /     課題の本文の編集・検証データ・運用
      task_ops
    drafts / bundles        下書き・AI 作問、束（zip）の取り込み
    learners / kc           受講者と習熟度、知識要素
    task_page               課題の 5 つのモジュールが共有する画面と保存
    common / grading_views  領域をまたぐ権限の確かめ・フォームの読み取り、
      / messages            評価器・観点の表示、保存後の文言

**呼ぶ順を変えるときは確かめる。** FastAPI は登録順にパスを照合するので
（`/tasks/new` と `/tasks/{task_id}` など）、先に登録したルートが後のルートを横取り
しうる ── `test_manage_route_order.py` が見張る（ルートの写しは行を並べ替えて
比べるので、順序の変化は捕まえない）。
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..urls import RedirectResponse
from .bundles import register as register_bundles
from .course import register as register_course
from .drafts import register as register_drafts
from .groups import register as register_groups
from .kc import register as register_kc
from .learners import register as register_learners
from .subjects import register as register_subjects
from .task_data import register as register_task_data
from .task_ops import register as register_task_ops
from .tasks import register as register_tasks
from .units import register as register_units
from .users import register as register_users

# ルータは `register()` の中で毎回作る。モジュール階層に置くと、
# `create_app` を 2 回呼んだときに同じ経路が二重に登録される
# （テストで複数のアプリを作ると起きる。FastAPI が Duplicate Operation ID を
# 警告して気づいた）。


def register(templates: Jinja2Templates) -> APIRouter:
    """テンプレートを束ねてルータを返す。呼ぶたびに新しいルータを作る。"""
    router = APIRouter(prefix="/manage")

    @router.get("", response_class=HTMLResponse)
    def index() -> Response:
        """コースの一覧は担当コース（`/`）に 1 つだけ置く。

        以前はここにも一覧があり、採点の入口（`/`）と管理の入口（`/manage`）で
        同じコースが 2 度並んでいた。**入口が 2 つあること自体が構成の
        分かりにくさの元**だったので、古い経路は残したまま集約する。
        """
        return RedirectResponse("/", status_code=303)

    register_users(router, templates)
    register_subjects(router, templates)
    register_course(router, templates)
    register_units(router, templates)
    register_bundles(router, templates)
    register_groups(router, templates)
    register_task_ops(router, templates)
    register_tasks(router, templates)
    register_task_data(router, templates)
    register_learners(router, templates)
    register_kc(router, templates)
    register_drafts(router, templates)

    return router
