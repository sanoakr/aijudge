"""動画の保存期間（ADR 0020）。

**起点は課題の締切であって、提出でも成績確定でもない。** 提出を起点にすると
同じ課題の動画が学生ごとに違う日に消え、早く出した学生のものが先に消える。
`Finalization` は提出 1 件ごとの記録で、採点完了から所定時間で自動確定する
（ADR 0014）── 学期末ではないので、提出起点と数日しか変わらない。

締切を起点にすると、同じ問題セットの課題は同じ締切を持つので
（`units:` の日程が投入時に各課題へ写る）、**回ごとにまとまって消える。**

**締切の無い課題だけは提出日から数える。** 起点が他に無いためで、期間も別
（1 年）である。当初は「起点が無いので消さない」としていたが、砂場・自習用に
数 GB の動画が積み上がるとディスクを圧迫する ── 消えない置き場所は、いずれ
容量の問題として運用に返ってくる。期間を長くし、受け付ける大きさを絞ることで
釣り合わせる（上限は学習者アプリ側・`MAX_VIDEO_BYTES_WITHOUT_DEADLINE`）。

ここが持つのは期限の計算だけである。何を消すかを選ぶのも、消すのも上位層
（`aijudge_admin.video_purge`）の仕事で、この層は I/O をしない。
"""

from __future__ import annotations

import calendar
from datetime import datetime

__all__ = [
    "PURGED_MESSAGE",
    "VIDEO_RETENTION_MONTHS",
    "VIDEO_RETENTION_MONTHS_WITHOUT_DEADLINE",
    "video_retention_expires_at",
    "video_retention_has_expired",
]

#: 締切から何ヶ月で消すか。
#:
#: 最も早い回でもその学期の疑義の後になる長さとして選んだ ── 後期の 10 月
#: 締切が 4 月、疑義は 2〜3 月（ADR 0020 の表）。日数ではなく月で数えるのは、
#: 「半年」が人の側の約束だからである。
VIDEO_RETENTION_MONTHS = 6

#: 消した動画を開こうとした人に出す文面。
#:
#: **両アプリで同じ文言を使う**（デモの帯と同じ理由）。学生と教員が違うことを
#: 言われると、どちらが本当か確かめることになる。事実は 1 つで、「保存期間を
#: 過ぎたので消した」である。
#:
#: **「見つかりません」と言わない。** 消去は運用の結果であって不具合ではなく、
#: 同じ文面にすると問い合わせ先まで同じに見える。
#: 締切の無い課題で、提出から何ヶ月で消すか。
#:
#: **締切のある課題より長い。** あちらは疑義までの時間で決まっているが、
#: こちらにはその暦が無い ── 自習用に出したものを半年で消す理由は無く、
#: 1 年なら「前の年度のもの」として説明できる。
VIDEO_RETENTION_MONTHS_WITHOUT_DEADLINE = 12

PURGED_MESSAGE = "保存期間を過ぎたため、この動画は消去されました。"


def _add_months(moment: datetime, months: int) -> datetime:
    """同じ日・同じ時刻の n ヶ月後。

    **月末は詰める。** 8 月 31 日の 6 ヶ月後は 2 月 31 日に存在しないので、
    その月の末日にする。`timedelta(days=183)` で代用しない ── 締切が月末に
    寄る運用で、消える日が月をまたいでずれる。
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def video_retention_expires_at(
    due_at: datetime | None, *, submitted_at: datetime | None = None
) -> datetime | None:
    """この動画を消してよくなる時刻。

    **締切があればそこから 6 ヶ月**、無ければ**提出から 1 年**である。
    締切のある課題で提出日を見ないのは、同じ課題の動画が学生ごとに違う日に
    消えないようにするため ── 締切が唯一の共通の起点である。

    どちらも無ければ `None`（消さない）。締切の無い課題の提出日は必ず
    あるので、実際にここへ来るのは下書きなど提出が成立していないものだけで
    ある。**起点が無いものは消さない**ほうへ倒す ── 消し過ぎは取り返せず、
    消し残しは次に消せる。
    """
    if due_at is not None:
        return _add_months(due_at, VIDEO_RETENTION_MONTHS)
    if submitted_at is not None:
        return _add_months(submitted_at, VIDEO_RETENTION_MONTHS_WITHOUT_DEADLINE)
    return None


def video_retention_has_expired(
    due_at: datetime | None, *, now: datetime, submitted_at: datetime | None = None
) -> bool:
    """この動画が保存期間を過ぎているか。起点が無ければ常に偽。"""
    expires_at = video_retention_expires_at(due_at, submitted_at=submitted_at)
    return expires_at is not None and now >= expires_at
