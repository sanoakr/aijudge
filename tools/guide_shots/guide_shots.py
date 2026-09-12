"""利用ガイド（docs/guide/）のスクリーンショットを撮り直す。

画面が変わるたびに手で撮り直していたものを 1 コマンドにする。**運用 DB からは
撮らない**（学生の提出物が入っている）。使い捨ての SQLite にサンプルの
アカウントと提出物を作り、そこから撮る。

    uv run --group guide-shots playwright install chromium   # 初回だけ
    uv run --group guide-shots python tools/guide_shots/guide_shots.py all

`all` は次を順に行う。個別に呼ぶこともできる（`seed` → `serve` の下で
`scenario` → `capture`）。

1. `seed`     ── 空の DB にコース・課題・教員 / TA / 学生・お試しコースを作る
2. `scenario` ── 学生が提出し、TA が blind 採点して確定し、学生が再確認を
                 依頼し、教員がそれに答える（ガイドの各画面が出る状態を作る）
3. `capture`  ── `docs/guide/images/` に PNG を書く

`scenario` と `capture` は Web・コンソール・ワーカーが動いている前提で、`all`
はそれをサブプロセスとして自分で起動する（`serve` 単体でも起動できる）。
採点には **docker サンドボックスと LLM が要る**。`AIJUDGE_SANDBOX` /
`AIJUDGE_SANDBOX_WORKDIR` / `AIJUDGE_LLM_BASE_URL` は環境変数のまま通す
（docs/RUNNING.md の「環境変数」）。既定の LLM はローカル ollama で、これは
学習者データを学外へ出さないという P7 の既定であり、ここでも変えない。

blind 採点の抽出率は 1.0 にした科目プロファイルの写しを使う（`AIJUDGE_PROFILES_DIR`
を使い捨て環境に向ける）。抽出は提出 ID のハッシュで決まり（ADR 0005）、本物の
0.05 では 4 件の提出のどれも抽出されないことのほうが多く、blind 採点の画面が
撮れない回が出る。率は測定の設定で、画面には出ない。

ID（コース・課題版・提出）はどれも DB から引く。撮り直すたびに ID は変わる
ので、値を書き写した台本は 1 回しか動かない ── それが前回の台本だった。

画像の切り抜き位置（`between=` の見出し）はテンプレートの h2 文言に依存する。
文言を変えたらここも直す。切り抜きが見つからなければ例外で止まり、黙って
古い画像が残ることはない。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
IMAGES_DIR = REPO_ROOT / "docs" / "guide" / "images"
TASKS_DIR = HERE / "tasks" / "ex01"
SOURCES_DIR = HERE / "sources"

WEB_PORT = 8080
CONSOLE_PORT = 8765
WEB = f"http://localhost:{WEB_PORT}"
CONSOLE = f"http://localhost:{CONSOLE_PORT}"

# サンプルアカウント。ガイドの本文に出る名前と一致させる。
PASSWORD = "Passw0rd!"
COURSE_CODE = "prog"
COURSE_TITLE = "プログラミング演習 I"
COURSE_TERM = "2026-後期"
SUBJECT_PROFILE = "cs_lang_c_intro"
SUBJECTS_DIR = REPO_ROOT / "subjects"
BLIND_RATE_LINE = ("blind_sample_rate: 0.05", "blind_sample_rate: 1.0")
INSTRUCTOR = ("sensei", "佐藤 花子")
ASSISTANT = ("ta01", "鈴木 太郎")
LEARNERS = (
    ("y230001", "山田 一郎"),
    ("y230002", "田中 花"),
    ("y230003", "高橋 健"),
    ("y230004", "伊藤 美咲"),
)
# `task import` で AI 評価器（読みやすさ）に渡す重み。0.3 だと決定的評価器
# 0.7 との配点が画面で「70% / 30%」と読める。
READABILITY_WEIGHT = "0.3"

CORRECT_SOURCE = TASKS_DIR / "p1" / "maxmin.c"
WRONG_SOURCE = SOURCES_DIR / "wrong.c"
# y230004 の「AI 待ち」画面用。正解だが y230001 と同じ内容ではない（同じ内容は
# 重複提出として弾かれ、新しい提出が作られない）。
WAITING_SOURCE = SOURCES_DIR / "maxmin2.c"

# 撮影時の見た目。学生画面は 1100 幅、コンソールは 1200 幅（サイドバー分）。
STUDENT_VIEWPORT = {"width": 1100, "height": 760}
CONSOLE_VIEWPORT = {"width": 1200, "height": 800}
MOBILE_VIEWPORT = {"width": 400, "height": 800}
CLIP_PADDING = 12
# ログイン画面だけは本番から撮る。使い捨て環境には OIDC が無く、「学内認証が
# 設定されていない」ときの画面になってしまう。ログイン前の画面なので学習者の
# データは含まない。
LOGIN_URL = "https://judge.math.ryukoku.ac.jp/login"
LOGIN_VIEWPORT = {"width": 1100, "height": 620}

# 採点待ちのポーリング。AI 評価は 30 秒前後、モデルが冷えていると数分かかる。
GRADING_TIMEOUT_SEC = 600
GRADING_POLL_SEC = 3
# 「AI 評価を待っています」を撮るまでの間。決定的評価（テスト実行）は数秒で
# 終わり、AI 評価はそれより長い。この間に撮ると両方の状態が 1 枚に入る。
WAITING_SHOT_DELAY_MS = 4000
SERVER_START_TIMEOUT_SEC = 60


# ── 環境 ───────────────────────────────────────────────


def state_env(state: Path) -> dict[str, str]:
    """使い捨て環境の環境変数。呼び出し側の環境（LLM・サンドボックス）は残す。"""
    env = dict(os.environ)
    env["AIJUDGE_DATABASE_URL"] = f"sqlite:///{state / 'guide.db'}"
    env["AIJUDGE_ARTIFACT_DIR"] = str(state / "artifacts")
    env["AIJUDGE_ADMIN_PASSWORD"] = PASSWORD
    env["AIJUDGE_PROFILES_DIR"] = str(state / "profiles")
    demo_course = state / "demo_course_id"
    if demo_course.is_file():
        env["AIJUDGE_DEMO_COURSE"] = demo_course.read_text().strip()
    return env


def run(args: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(
        ["uv", "run", *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"失敗: {' '.join(args)}")
    return result.stdout


def db(state: Path) -> sqlite3.Connection:
    return sqlite3.connect(state / "guide.db")


def query_one(state: Path, sql: str, *params: Any) -> str:
    row = db(state).execute(sql, params).fetchone()
    if row is None:
        raise SystemExit(f"DB に見つからない: {sql} {params}")
    return str(row[0])


def course_id(state: Path, code: str) -> str:
    return query_one(state, "select id from courses where code = ?", code)


def submission_id(state: Path, login: str, attempt: int) -> str:
    return query_one(
        state,
        "select s.id from submissions s join users u on u.id = s.learner_id"
        " where u.login = ? and s.attempt = ?",
        login,
        attempt,
    )


# ── seed ───────────────────────────────────────────────


def seed(state: Path) -> None:
    if state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True)
    shutil.copytree(SUBJECTS_DIR, state / "profiles")
    profile = state / "profiles" / f"{SUBJECT_PROFILE}.yaml"
    text = profile.read_text()
    if BLIND_RATE_LINE[0] not in text:
        raise SystemExit(f"{profile.name} に {BLIND_RATE_LINE[0]!r} がない ── 定数を直す")
    profile.write_text(text.replace(BLIND_RATE_LINE[0], BLIND_RATE_LINE[1]))
    env = state_env(state)
    run(["alembic", "upgrade", "head"], env)
    admin = ["aijudge-admin"]
    run(
        [
            *admin,
            "course",
            "create",
            "--code",
            COURSE_CODE,
            "--title",
            COURSE_TITLE,
            "--term",
            COURSE_TERM,
            "--profile",
            SUBJECT_PROFILE,
        ],
        env,
    )
    course = course_id(state, COURSE_CODE)
    run(
        [
            *admin,
            "task",
            "import",
            "--course",
            course,
            "--dir",
            str(TASKS_DIR),
            "--readability-weight",
            READABILITY_WEIGHT,
        ],
        env,
    )
    staff = [(INSTRUCTOR, "instructor"), (ASSISTANT, "assistant")]
    staff += [(learner, "learner") for learner in LEARNERS]
    for (login, name), role in staff:
        run(
            [*admin, "staff", "--login", login, "--name", name, "--course", course, "--role", role],
            env,
        )
    # 教員はテナント管理者でもある（コースの追加・利用者・科目プロファイルの画面）。
    # `--role admin` を `--course` 無しで渡すとテナント単位の属性が付く（#128）。
    run([*admin, "staff", "--login", INSTRUCTOR[0], "--role", "admin"], env)
    run([*admin, "kc", "seed", "--namespace", "demo"], env)
    run([*admin, "--artifacts", env["AIJUDGE_ARTIFACT_DIR"], "demo", "seed"], env)
    (state / "demo_course_id").write_text(course_id(state, "demo"))
    print(f"seed: {state}")


# ── serve ──────────────────────────────────────────────


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return
        except urllib.error.HTTPError:
            return  # 応答している（リダイレクト等）
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise SystemExit(f"{url} が起動しない")


@contextmanager
def serve(state: Path) -> Iterator[None]:
    """Web・コンソール・2 レーンのワーカーを起動し、抜けるときに止める。"""
    env = state_env(state)
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    commands = {
        "web": ["aijudge-web", "--port", str(WEB_PORT)],
        "review": ["aijudge-review", "--port", str(CONSOLE_PORT)],
        "worker-det": ["aijudge-worker", "--phase", "deterministic", "--name", "det"],
        "worker-ai": ["aijudge-worker", "--phase", "ai", "--name", "ai1"],
    }
    procs = []
    for name, args in commands.items():
        log = (logs / f"{name}.log").open("w")
        procs.append(
            subprocess.Popen(
                ["uv", "run", *args], cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        )
    try:
        _wait_http(f"{WEB}/login")
        _wait_http(f"{CONSOLE}/login")
        yield
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── ブラウザ操作 ──────────────────────────────────────────


def login(page: Any, base: str, user: str) -> None:
    page.goto(f"{base}/auth/local")
    page.fill("#login", user)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")


def first_task_url(page: Any, course: str) -> str:
    """コース画面の最初の課題（ex01 p1）。課題版 ID はここから拾う。"""
    page.goto(f"{WEB}/courses/{course}")
    href = page.locator("a[href^='/tasks/']").first.get_attribute("href")
    return f"{WEB}{href}"


def submit(page: Any, task_url: str, source: Path) -> None:
    page.goto(task_url)
    page.set_input_files("#upload", str(source))
    page.click("form[action$='/submit'] button[type=submit]")


def blind_mark(page: Any, submission: str, *, correctness: int, readability: int) -> None:
    page.goto(f"{CONSOLE}/review/{submission}/blind")
    page.check(f"input[name=level_correctness][value='{correctness}']")
    page.check(f"input[name=level_readability][value='{readability}']")
    page.click("form[action$='/blind'] button[type=submit]")


def wait_graded(state: Path, submission: str) -> None:
    """両レーンのジョブが `done` になるまで待つ。

    学生画面の「AI 評価を待っています」を見る手もあるが、AI ジョブが待ち行列に
    並んだだけの間はその帯が出ず、早く抜けてしまった。ジョブの状態のほうが
    採点の進み具合そのものである。
    """
    deadline = time.monotonic() + GRADING_TIMEOUT_SEC
    while time.monotonic() < deadline:
        rows = (
            db(state)
            .execute("select phase, state from grading_jobs where submission_id = ?", (submission,))
            .fetchall()
        )
        states = {phase: job_state for phase, job_state in rows}
        if "failed" in states.values():
            raise SystemExit(f"{submission} の採点が失敗した: {states}（logs/ を見る）")
        if states and all(job_state == "done" for job_state in states.values()):
            return
        time.sleep(GRADING_POLL_SEC)
    raise SystemExit(f"{submission} の採点が {GRADING_TIMEOUT_SEC} 秒で終わらない")


# ── scenario ───────────────────────────────────────────


def scenario(state: Path, playwright: Any) -> None:
    browser = playwright.chromium.launch()

    def page_as(base: str, user: str, **viewport: int) -> Any:
        context = browser.new_context(locale="ja-JP", viewport=viewport or CONSOLE_VIEWPORT)
        page = context.new_page()
        login(page, base, user)
        return page

    course = course_id(state, COURSE_CODE)

    # 学生 3 名が提出。y230001 は正解、y230002 / y230003 は平均を出さない誤答。
    submissions = (
        ("y230001", CORRECT_SOURCE),
        ("y230002", WRONG_SOURCE),
        ("y230003", WRONG_SOURCE),
    )
    task_url = ""
    for user, source in submissions:
        page = page_as(WEB, user)
        task_url = first_task_url(page, course)
        submit(page, task_url, source)
        page.context.close()
    for user, _ in submissions:
        wait_graded(state, submission_id(state, user, 1))

    # 教員が日程を入れる。撮影日を基準にした相対日付（絶対日付は撮るたびに古びる）。
    today = datetime.now().replace(second=0, microsecond=0)
    page = page_as(CONSOLE, INSTRUCTOR[0])
    page.goto(f"{CONSOLE}/manage/courses/{course}/units/ex01")
    page.fill("#opens_at", (today - timedelta(days=3)).strftime("%Y-%m-%dT09:00"))
    page.fill("#due_at", (today + timedelta(days=7)).strftime("%Y-%m-%dT23:59"))
    page.fill("#accepts_until", (today + timedelta(days=14)).strftime("%Y-%m-%dT23:59"))
    page.click("text=日程を保存")
    page.context.close()

    # y230003 が再確認を依頼し、y230002 は正解を出し直す。
    page = page_as(WEB, "y230003")
    page.goto(f"{WEB}/submissions/{submission_id(state, 'y230003', 1)}")
    page.fill(
        "#reason",
        "出力の正しさが「未達」になっていますが、入力例 1 と 2 は手元では正しく出力"
        "できています。平均値の桁の扱いを確認していただけますか。",
    )
    page.click("form[action$='/request-review'] button[type=submit]")
    page.context.close()
    page = page_as(WEB, "y230002")
    submit(page, task_url, CORRECT_SOURCE)
    page.context.close()
    wait_graded(state, submission_id(state, "y230002", 2))

    # 抽出率 1.0 なので、開示する提出はどれも先に blind 採点が要る。
    # TA が y230001 を blind 採点してから確定（AI の判定どおり）。y230002 の
    # 1 回目はガイドの「確認の画面」で開示だけして見せる提出で、確定はしない。
    page = page_as(CONSOLE, ASSISTANT[0], width=1200, height=1600)
    first = submission_id(state, "y230001", 1)
    blind_mark(page, first, correctness=3, readability=2)
    blind_mark(page, submission_id(state, "y230002", 1), correctness=0, readability=0)
    page.goto(f"{CONSOLE}/review/{first}/reveal")
    page.fill(
        "#comment",
        "テストケース 5 件すべて通過を確認しました。変数名は役割を表しており、判定は妥当です。",
    )
    page.click("form[action$='/finalize'] button[type=submit]")
    page.context.close()

    # 教員が y230003 の再確認の依頼に答える（判定は据え置き）。
    page = page_as(CONSOLE, INSTRUCTOR[0], width=1200, height=1600)
    questioned = submission_id(state, "y230003", 1)
    blind_mark(page, questioned, correctness=0, readability=0)
    page.goto(f"{CONSOLE}/review/{questioned}/reveal")
    page.fill(
        "#comment",
        "提出されたプログラムは平均値を出力していません（printf が最大値と最小値の 2 つ"
        "だけ）。入力例 1 の期待出力は「2 2 2.000」で、平均値の桁ではなく項目の不足が"
        "原因です。判定は据え置きます。",
    )
    page.click("form[action$='/finalize'] button[type=submit]")
    page.context.close()
    browser.close()
    print("scenario: done")


# ── capture ────────────────────────────────────────────


class Shooter:
    """1 ページ分の撮影。`between` は見出しから次の見出しまでを切り抜く。"""

    def __init__(self, page: Any) -> None:
        self.page = page

    def _y(self, selector: str) -> float:
        box = self.page.locator(selector).first.bounding_box()
        if box is None:
            raise SystemExit(f"切り抜きの目印が見つからない: {selector}")
        return float(box["y"])

    def shot(
        self,
        name: str,
        *,
        full: bool = False,
        selector: str | None = None,
        between: tuple[str, str | None] | None = None,
        pad: int = CLIP_PADDING,
        height: int | None = None,
    ) -> None:
        path = str(IMAGES_DIR / f"{name}.png")
        if selector:
            self.page.locator(selector).first.screenshot(path=path)
            return
        if between:
            top = self._y(between[0]) - pad
            bottom = (
                self._y(between[1]) - pad
                if between[1]
                else float(self.page.evaluate("document.documentElement.scrollHeight"))
            )
            if height:
                bottom = min(bottom, top + height)
            clip = {
                "x": 0,
                "y": top,
                "width": self.page.viewport_size["width"],
                "height": bottom - top,
            }
            self.page.screenshot(path=path, full_page=True, clip=clip)
            return
        self.page.screenshot(path=path, full_page=full)


def capture(state: Path, playwright: Any, login_url: str) -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    browser = playwright.chromium.launch()

    def new_page(viewport: dict[str, int]) -> Any:
        return browser.new_context(
            viewport=viewport, locale="ja-JP", device_scale_factor=1
        ).new_page()

    course = course_id(state, COURSE_CODE)
    demo = course_id(state, "demo")
    sub_ng = submission_id(state, "y230002", 1)
    sub_q = submission_id(state, "y230003", 1)
    sub_retry = submission_id(state, "y230002", 2)

    # ── 学生 ──
    page = new_page(LOGIN_VIEWPORT)
    page.goto(login_url)
    Shooter(page).shot("st-login")
    page.context.close()

    page = new_page(STUDENT_VIEWPORT)
    s = Shooter(page)
    login(page, WEB, "y230002")
    s.shot("st-home", between=("main h1", None), height=330)
    task_url = first_task_url(page, course)
    s.shot("st-course", between=("main h1", "p.desc, main > p"), pad=16)
    page.goto(task_url)
    s.shot("st-task", full=True)
    s.shot("st-task-submit", between=("h2:has-text('提出する')", "h2:has-text('これまでの提出')"))
    s.shot("st-task-history", between=("h2:has-text('これまでの提出')", None), height=260)
    page.goto(f"{WEB}/submissions/{sub_retry}")
    s.shot("st-result", between=("main h1", "h2:has-text('採点の再確認')"))
    s.shot(
        "st-result-contest", between=("h2:has-text('採点の再確認')", "h2:has-text('提出したもの')")
    )
    page.context.close()

    page = new_page(STUDENT_VIEWPORT)
    login(page, WEB, "y230003")
    page.goto(f"{WEB}/submissions/{sub_q}")
    Shooter(page).shot("st-result-answered", between=("main h1", "h2:has-text('提出したもの')"))
    page.context.close()

    page = new_page(MOBILE_VIEWPORT)
    login(page, WEB, "y230002")
    page.goto(f"{WEB}/courses/{course}")
    Shooter(page).shot("st-mobile")
    page.context.close()

    page = new_page(STUDENT_VIEWPORT)
    login(page, WEB, "y230002")
    page.goto(f"{WEB}/courses/{demo}")
    Shooter(page).shot("st-demo", between=("main h1", None), height=520)
    page.context.close()

    # ── TA ──
    page = new_page(CONSOLE_VIEWPORT)
    s = Shooter(page)
    login(page, CONSOLE, ASSISTANT[0])
    s.shot("ta-home")
    page.goto(f"{CONSOLE}/courses/{course}")
    s.shot("ta-course")
    page.goto(f"{CONSOLE}/courses/{course}/submissions")
    s.shot("ta-submissions", full=True)
    page.goto(f"{CONSOLE}/courses/{course}/queue")
    s.shot("ta-queue", between=("main h1", "main p.desc:below(table)"), height=480)
    page.goto(f"{CONSOLE}/review/{sub_ng}/reveal")
    s.shot("ta-reveal", full=True)
    page.set_viewport_size({"width": 1200, "height": 1500})
    page.goto(f"{CONSOLE}/review/{sub_ng}/reveal")
    s.shot("ta-reveal-form", selector="form[action$='/finalize']")
    page.set_viewport_size(CONSOLE_VIEWPORT)
    page.goto(f"{CONSOLE}/courses/{course}/blind")
    s.shot("ta-blind", full=True)
    page.goto(f"{CONSOLE}/courses/{course}/finalize")
    s.shot("ta-finalize", full=True)
    page.goto(f"{CONSOLE}/manage/courses/{course}/units/ex01")
    s.shot("ta-unit", full=True)
    page.context.close()

    # blind 採点のフォームは、まだ blind されていない提出（y230002 の 2 回目）で撮る。
    page = new_page({"width": 1200, "height": 1600})
    login(page, CONSOLE, ASSISTANT[0])
    page.goto(f"{CONSOLE}/review/{sub_retry}/blind")
    Shooter(page).shot("ta-blind-form", full=True)
    page.context.close()

    # ── 教員 ──
    page = new_page(CONSOLE_VIEWPORT)
    s = Shooter(page)
    login(page, CONSOLE, INSTRUCTOR[0])
    s.shot("in-home", between=("main h1", "h2:has-text('受講しているコース')"))
    s.shot(
        "in-home-newcourse",
        between=("h2:has-text('コースを追加する')", "h2:has-text('ローカル利用者')"),
    )
    page.goto(f"{CONSOLE}/courses/{course}")
    s.shot("in-course")
    page.goto(f"{CONSOLE}/manage/courses/{course}/units/ex01")
    s.shot("in-unit-schedule", between=("h2:has-text('日程')", "h2:has-text('成績の自動確定')"))
    s.shot("in-unit-tasks", between=("h2:has-text('この問題セットの課題')", "h3:has-text('zip')"))
    s.shot("in-unit-bundle", between=("h3:has-text('zip')", "h2:has-text('片付ける')"))
    s.shot("in-unit-generate", between=("h2:has-text('AI に課題')", None), height=700)
    page.goto(f"{CONSOLE}/manage/courses/{course}/units/ex01/tasks/new")
    s.shot("in-task-new", full=True)
    page.goto(f"{CONSOLE}/manage/courses/{course}/drafts")
    s.shot("in-drafts", full=True)
    page.goto(f"{CONSOLE}/manage/courses/{course}")
    s.shot(
        "in-settings-autofinalize",
        between=("h2:has-text('成績の自動確定')", "h2:has-text('提出できるファイル形式')"),
    )
    s.shot(
        "in-settings-rubric", between=("h2:has-text('共通ルーブリック')", "h2:has-text('採点設定')")
    )
    s.shot(
        "in-settings-grading",
        between=("h2:has-text('採点設定')", "h2:has-text('課題文に貼る画像')"),
    )
    page.goto(f"{CONSOLE}/manage/courses/{course}/basics")
    s.shot("in-basics", full=True)
    page.goto(f"{CONSOLE}/manage/courses/{course}/enrolments")
    s.shot("in-enrolments", full=True)
    page.goto(f"{CONSOLE}/manage/courses/{course}/kc")
    s.shot("in-kc", full=True)
    page.goto(f"{CONSOLE}/courses/{course}/finalize")
    page.click("summary")
    page.wait_for_timeout(300)
    s.shot("in-finalize", full=True)
    page.goto(f"{CONSOLE}/manage/users")
    s.shot("ad-users", full=True)
    page.goto(f"{CONSOLE}/manage/subjects")
    s.shot("ad-subjects", full=True)
    page.goto(f"{CONSOLE}/manage/oidc-settings")
    s.shot("ad-oidc", full=True)
    page.context.close()

    # 提出直後（テスト実行は済み、AI 待ち）。**最後に撮る** ── この提出は
    # しばらく「保留」のままで、先に撮る一覧に混ざると採点中の行が写る。
    page = new_page(STUDENT_VIEWPORT)
    login(page, WEB, "y230004")
    submit(page, task_url, WAITING_SOURCE)
    page.wait_for_timeout(WAITING_SHOT_DELAY_MS)
    page.reload()
    Shooter(page).shot("st-result-waiting", between=("main h1", "h2:has-text('採点の再確認')"))
    page.context.close()

    browser.close()
    print(f"capture: {IMAGES_DIR}")


# ── main ───────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("all", "seed", "serve", "scenario", "capture"))
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("TMPDIR", "/tmp")) / "aijudge-guide-shots",
        help="使い捨て DB・提出物・ログの置き場所（seed で作り直す）",
    )
    parser.add_argument(
        "--login-url",
        default=LOGIN_URL,
        help="ログイン画面を撮る URL（既定は本番。使い捨て環境には学内認証が無い）",
    )
    args = parser.parse_args()
    state: Path = args.state.resolve()

    if args.command == "seed":
        seed(state)
        return
    if args.command == "serve":
        with serve(state):
            print(f"serve: {WEB} / {CONSOLE}（Ctrl-C で止める）")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if args.command == "scenario":
            scenario(state, playwright)
        elif args.command == "capture":
            capture(state, playwright, args.login_url)
        else:
            seed(state)
            with serve(state):
                scenario(state, playwright)
                capture(state, playwright, args.login_url)


if __name__ == "__main__":
    main()
