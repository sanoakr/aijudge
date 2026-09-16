"""習熟度の見せ方は 1 か所（#335）。

教員が受講者 1 人を見る画面と、学習者が自分を見る画面は**同じ問いに答える**
── どの知識要素がどれだけ身に付いているか、その根拠は何か。別々に書くと、
片方を直した日にもう片方だけが古くなる（配色と共通部品を 1 か所に置いたのと
同じ理由・#184）。

ここで固定するのは 2 つ。**同じ断片を読んでいる**ことと、**畳む形（推定の
読み出し）がアプリの外にある**ことである。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE = REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole"
LEARNER = REPO_ROOT / "apps" / "studentweb" / "src" / "aijudge_studentweb"
SHARED = REPO_ROOT / "packages" / "webui" / "src" / "aijudge_webui" / "templates"

FRAGMENT = "_mastery_rows.html"


def test_the_fragment_is_shared() -> None:
    assert (SHARED / FRAGMENT).is_file(), "共有の断片が無い"


def test_both_screens_include_it() -> None:
    """**同じものを描く。** 片方だけ手で書くと、そちらが古くなる。"""
    console = (CONSOLE / "templates" / "manage_mastery_learner.html").read_text(encoding="utf-8")
    learner = (LEARNER / "templates" / "mastery.html").read_text(encoding="utf-8")

    for name, body in (("教員", console), ("学習者", learner)):
        assert FRAGMENT in body, f"{name}の画面が共有の断片を読んでいない"


def test_neither_screen_lays_out_the_rows_itself() -> None:
    """行の組み立てを画面に書かない ── 断片が持つ仕事である。"""
    console = (CONSOLE / "templates" / "manage_mastery_learner.html").read_text(encoding="utf-8")
    learner = (LEARNER / "templates" / "mastery.html").read_text(encoding="utf-8")

    for name, body in (("教員", console), ("学習者", learner)):
        assert "kcbar-fill" not in body, f"{name}の画面が図を自前で描いている"
        assert "human_verified" not in body, f"{name}の画面が根拠を自前で並べている"


def test_the_shaping_lives_outside_the_apps() -> None:
    """**畳む形はパッケージに置く。** アプリどうしが import し合わない
    （合成ルートは互いを知らない）。
    """
    portfolio = REPO_ROOT / "packages" / "skill" / "src" / "aijudge_skill" / "portfolio.py"
    assert portfolio.is_file()

    learner_app = (LEARNER / "app.py").read_text(encoding="utf-8")
    assert "aijudge_reviewconsole" not in learner_app, "学生アプリが教員コンソールを読んでいる"
    assert "aijudge_skill.portfolio" in learner_app


def test_the_shaping_does_not_know_about_courses() -> None:
    """習熟度はテナント単位の値。**コースの概念を S7 に持ち込まない**（P6）。

    根拠がどのコースから来たかは、呼び出し側が解決して渡す。
    """
    portfolio = (
        REPO_ROOT / "packages" / "skill" / "src" / "aijudge_skill" / "portfolio.py"
    ).read_text(encoding="utf-8")

    assert "CourseId" not in portfolio
    assert "uow" not in portfolio, "保存先に触れている"
