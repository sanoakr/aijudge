"""入口は 2 つある。**同じ構図で、地の色だけが違う**（#326）。

学習者の入口と管理側の入口は同じ機関の同じシステムなので、構図（銘・札・
押す場所）を変えない ── 教員が学習者の画面を開いたときに別のサービスに
見えてはいけない。

**そのうえで、どちらの扉かは分からなければならない。** 教員は 2 つの画面を
並べて開き、学習者に何が見えているかを確かめながら作業する。区別は地の色
だけで付ける（`.gate-admin`）。

ここで固定するのは 3 つ。管理側の 2 つの入口が朱の面であること、学習者の
入口がそこに巻き込まれていないこと、そして夜の値が**両方の経路**に居ること
（`test_the_viewer_can_choose_day_or_night.py` と同じ壊れ方をするため ──
片方だけ直すと、自分で夜を選んだ人と OS が夜の人とで違う色を見る）。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE = REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "templates"
LEARNER = REPO_ROOT / "apps" / "studentweb" / "src" / "aijudge_studentweb" / "templates"
BASE_CSS = REPO_ROOT / "packages" / "webui" / "src" / "aijudge_webui" / "assets" / "base.css"

ADMIN_ENTRANCES = (CONSOLE / "login.html", CONSOLE / "login_local.html")


def test_both_admin_entrances_are_the_red_ground() -> None:
    """管理側は **2 つとも**朱の面。

    以前 `login_local.html` だけが本文の段組みの中の素の枠で、同じシステムの
    同じ入口が 2 種類あった ── 管理者が使うのはこちらなので、揃っていない側が
    よりによって運用の入口だった。
    """
    for path in ADMIN_ENTRANCES:
        body = path.read_text(encoding="utf-8")
        assert 'class="gate gate-admin"' in body, f"{path.name}: 管理側の入口が朱の面になっていない"
        assert 'class="gate-mark"' in body, f"{path.name}: 入口の構図（銘）が無い"
        assert 'class="gate-card"' in body, f"{path.name}: 入口の構図（札）が無い"


def test_the_learner_entrance_stays_indigo() -> None:
    """学習者の入口は藍のまま。**区別が付かなければ、色を分けた意味が無い。**"""
    body = (LEARNER / "login.html").read_text(encoding="utf-8")
    assert 'class="gate"' in body
    assert "gate-admin" not in body, "学習者の入口まで朱になっている"


def test_the_red_ground_has_a_night_value_on_both_paths() -> None:
    """夜の値は OS 追従と明示の選択の**両方**に要る。

    片方だけだと、自分で夜を選んだ人と OS が夜の人とで別の色を見る
    （`test_the_viewer_can_choose_day_or_night.py` と同じ壊れ方）。
    """
    css = BASE_CSS.read_text(encoding="utf-8")
    assert ".gate-admin{" in css, "管理側の入口の配色が定義されていない"
    # 昼・OS 追従・明示の選択で 3 回。
    assert css.count(".gate-admin{") == 3, (
        '.gate-admin は昼・@media・[data-theme="dark"] の 3 か所に要る'
    )
    night = "--head-bg:#2a100c"
    assert css.count(night) == 2, f"{night} が夜の 2 経路に揃っていない"
