"""IDE の負荷試験（設計書 §10・§12 の段階 3）。N 名の学習者の振る舞いを模す。

    uv run python tools/ide_load/load.py http://127.0.0.1:8091 --learners 150 --minutes 5

**使い捨ての環境にだけ向ける。DB は PostgreSQL にすること** ── 開発用の SQLite は
1 本の接続をスレッド間で共有するので、同時のログインだけで 500 になる。 `seed.py` で作った学習者でログインし、1 人
ずつ次を繰り返す（設計書の見積もりの前提と同じ）。

- 10 秒ごと: 行動記録を 1 束送る（打鍵 30 件ほど）と、自動保存
- 約 45 秒ごと（ばらつきあり）: 実行して、結果が出るまで 0.5 秒ごとに問い合わせる
- 最後に 1 回: 3 問とも提出する

測るもの:

- 実行待ち: 要求から結果までの時間（p50・p95・最大）、期限切れ・失敗の件数
- 行動記録: 1 回の送信の応答時間（p95）と、受け取られなかった割合
- 自動保存・提出の応答時間と失敗

**数値は手元の構成のものである。** runner の本数（K）を決めるのは、運用機で
測った値だけにする（設計書 §10）。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from dataclasses import dataclass, field

import httpx

COURSE = "crs_" + "1" * 32
UNIT = "load01"
PROBLEMS = 3
TASK_VERSIONS = [f"tsv_{n:032x}" for n in range(1, PROBLEMS + 1)]
SOURCE = """#include <stdio.h>
int main(void) {
    int n, x, mx = -1000000, mn = 1000000; double s = 0;
    do { scanf("%d", &n); } while (n < 1);
    for (int i = 0; i < n; i++) {
        scanf("%d", &x); if (x > mx) mx = x; if (x < mn) mn = x; s += x;
    }
    printf("%d %d %.3f\\n", mx, mn, s / n);
    return 0;
}
"""
BATCH_SECONDS = 10.0
RUN_MEAN_SECONDS = 45.0
POLL_SECONDS = 0.5


@dataclass
class Stats:
    run_wait: list[float] = field(default_factory=list)
    run_outcomes: dict[str, int] = field(default_factory=dict)
    run_refused: dict[int, int] = field(default_factory=dict)
    activity_latency: list[float] = field(default_factory=list)
    activity_failed: int = 0
    activity_sent: int = 0
    save_latency: list[float] = field(default_factory=list)
    save_failed: int = 0
    submit_latency: list[float] = field(default_factory=list)
    submit_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def count(self, table: dict, key) -> None:  # type: ignore[type-arg]
        table[key] = table.get(key, 0) + 1


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def learner(index: int, args: argparse.Namespace, stats: Stats, deadline: float) -> None:
    async with httpx.AsyncClient(base_url=args.base, timeout=60.0) as client:
        login = await client.post(
            "/auth/local",
            data={"login": f"load{index:03d}", "password": args.password},
            follow_redirects=False,
        )
        if login.status_code != 303:
            stats.errors.append(f"login load{index:03d}: {login.status_code}")
            return
        page = await client.get(f"/courses/{COURSE}/ide", params={"unit": UNIT})
        if page.status_code != 200:
            stats.errors.append(f"ide page: {page.status_code}")
            return
        started = await client.post(
            "/ide/sessions",
            json={"course_id": COURSE, "unit": UNIT, "consent": True, "user_agent": "load"},
        )
        session_id = started.json().get("session_id")
        seq = 0
        # 開始の瞬間の集中を少しばらす（一斉に同じ秒で送らない）。
        await asyncio.sleep(random.uniform(0, BATCH_SECONDS))
        next_run = time.monotonic() + random.expovariate(1 / args.run_mean)
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            tab = random.randrange(PROBLEMS)
            events = [
                {
                    "type": "edit",
                    "t": (time.monotonic() - t0) * 1000,
                    "tab": tab,
                    "off": i,
                    "del": 0,
                    "ins": "a",
                }
                for i in range(30)
            ]
            begin = time.monotonic()
            try:
                response = await client.post(
                    "/ide/activity",
                    json={
                        "session_id": session_id,
                        "seq": seq,
                        "client_time": time.time() * 1000,
                        "events": events,
                    },
                )
                stats.activity_sent += 1
                if response.status_code == 200:
                    seq += 1
                else:
                    stats.activity_failed += 1
                stats.activity_latency.append(time.monotonic() - begin)
            except httpx.HTTPError as exc:
                stats.activity_failed += 1
                stats.errors.append(f"activity: {exc!r}")

            begin = time.monotonic()
            try:
                saved = await client.put(
                    f"/ide/tasks/{TASK_VERSIONS[tab]}/buffer",
                    json={"suffix": ".c", "source": SOURCE},
                )
                if saved.status_code != 200:
                    stats.save_failed += 1
                stats.save_latency.append(time.monotonic() - begin)
            except httpx.HTTPError:
                stats.save_failed += 1

            if time.monotonic() >= next_run:
                await run_once(client, tab, stats)
                next_run = time.monotonic() + random.expovariate(1 / args.run_mean)
            await asyncio.sleep(BATCH_SECONDS)

        for tv in TASK_VERSIONS:
            begin = time.monotonic()
            try:
                submitted = await client.post(
                    f"/ide/tasks/{tv}/submit",
                    json={"suffix": ".c", "source": SOURCE + f"// {index}\n"},
                )
                if submitted.status_code != 200:
                    stats.submit_failed += 1
                stats.submit_latency.append(time.monotonic() - begin)
            except httpx.HTTPError:
                stats.submit_failed += 1


async def run_once(client: httpx.AsyncClient, tab: int, stats: Stats) -> None:
    begin = time.monotonic()
    response = await client.post(
        f"/ide/tasks/{TASK_VERSIONS[tab]}/run",
        json={"suffix": ".c", "source": SOURCE, "stdin": "3 1 2 3\n"},
    )
    if response.status_code != 202:
        stats.count(stats.run_refused, response.status_code)
        return
    run_id = response.json()["id"]
    while True:
        await asyncio.sleep(POLL_SECONDS)
        state = (await client.get(f"/ide/runs/{run_id}")).json()
        if state["state"] not in ("queued", "running"):
            stats.run_wait.append(time.monotonic() - begin)
            stats.count(stats.run_outcomes, state["state"])
            return


def report(stats: Stats, args: argparse.Namespace, elapsed: float) -> None:
    def line(name: str, values: list[float]) -> str:
        if not values:
            return f"{name}: (なし)"
        return (
            f"{name}: n={len(values)} p50={statistics.median(values):.3f}s "
            f"p95={pct(values, 0.95):.3f}s max={max(values):.3f}s"
        )

    print(f"\n== {args.learners} 名 × {elapsed / 60:.1f} 分 ==")
    print(line("実行待ち（要求→結果）", stats.run_wait))
    print(f"  結果: {stats.run_outcomes}  断られた: {stats.run_refused}")
    print(line("行動記録の送信", stats.activity_latency))
    lost = stats.activity_failed / stats.activity_sent if stats.activity_sent else float("nan")
    print(f"  受け取られなかった: {stats.activity_failed}/{stats.activity_sent}（{lost:.2%}）")
    print(line("自動保存", stats.save_latency), f" 失敗 {stats.save_failed}")
    print(line("提出", stats.submit_latency), f" 失敗 {stats.submit_failed}")
    rate = len(stats.run_wait) / elapsed if elapsed else 0
    print(f"実行の頻度: {rate:.2f} 回/秒、行動記録: {stats.activity_sent / elapsed:.1f} 回/秒")
    for error in stats.errors[:10]:
        print("  エラー:", error)


async def main() -> None:
    parser = argparse.ArgumentParser(description="IDE の負荷試験（使い捨ての環境にだけ向ける）")
    parser.add_argument("base", help="学生画面の URL（例 http://127.0.0.1:8091）")
    parser.add_argument("--learners", type=int, default=150)
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--password", default="load-test-pass")
    parser.add_argument(
        "--run-mean",
        type=float,
        default=RUN_MEAN_SECONDS,
        help="1 人が実行する平均間隔（秒）。既定は設計書の見積もりの 45 秒",
    )
    args = parser.parse_args()

    stats = Stats()
    started = time.monotonic()
    deadline = started + args.minutes * 60
    await asyncio.gather(*(learner(i, args, stats, deadline) for i in range(1, args.learners + 1)))
    report(stats, args, time.monotonic() - started)


if __name__ == "__main__":
    asyncio.run(main())
