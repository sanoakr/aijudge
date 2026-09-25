"""行動記録の要約（設計書 §7 の「提出ごとの要約」）。

教員が一覧で見る数字だけを出す。**判定はしない** ── 外からの貼り付けが
多いことも、画面を離れていた時間が長いことも、それだけでは何も意味しない
（教科書のサンプルを貼る、トイレに立つ）。数字は再生（ビューア）で経過を
確かめるための入口である。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass
class ActivitySummary:
    edits: int = 0
    # 打鍵で入れた文字数（貼り付けと読み込みを除く）。
    typed_chars: int = 0
    pastes_external: int = 0
    pasted_external_chars: int = 0
    pastes_internal: int = 0
    # 最大の外からの貼り付け（文字数）。1 回で答え全体を貼ったかの目安。
    largest_external_paste: int = 0
    copies_from_statement: int = 0
    file_loads: int = 0
    runs: int = 0
    submits: int = 0
    # 画面を離れていた合計（秒）と回数。blur→focus と hidden→visible の区間。
    away_seconds: float = 0.0
    away_count: int = 0
    duration_seconds: float = 0.0


def summarize(events: Iterable[dict[str, Any]]) -> ActivitySummary:
    """1 回のセッションのイベント列（時刻順）を要約する。"""
    summary = ActivitySummary()
    away_since: float | None = None
    # 貼り付けは「paste」と、それが起こす「edit」の両方に現れる。打鍵の文字数
    # から貼り付け分を除くため、直前の貼り付けの長さを覚えておく。
    pending_paste = 0
    first_t: float | None = None
    last_t = 0.0

    for event in events:
        kind = event.get("type")
        t = float(event.get("t", 0))
        first_t = t if first_t is None else first_t
        last_t = max(last_t, t)
        if kind == "edit":
            summary.edits += 1
            inserted = len(str(event.get("ins", "")))
            if pending_paste and inserted == pending_paste:
                pending_paste = 0
            else:
                summary.typed_chars += inserted
        elif kind == "paste":
            length = int(event.get("len", 0))
            if event.get("origin") == "internal":
                summary.pastes_internal += 1
            else:
                summary.pastes_external += 1
                summary.pasted_external_chars += length
                summary.largest_external_paste = max(summary.largest_external_paste, length)
            # Monaco は貼り付けの edit を paste の前に出す。次の edit ではなく、
            # 直前に数えた打鍵から差し引く。
            if summary.typed_chars >= length:
                summary.typed_chars -= length
            else:
                pending_paste = length
        elif kind == "copy" and event.get("from") == "statement":
            summary.copies_from_statement += 1
        elif kind == "file_load":
            summary.file_loads += 1
        elif kind == "run" and event.get("stage") == "request":
            summary.runs += 1
        elif kind == "submit" or (kind == "attach" and event.get("stage") == "submit"):
            # 画像・PDF の提出も提出として数える（`attach` の `submit` 段）。
            summary.submits += 1
        elif kind in ("blur", "visibility"):
            leaving = kind == "blur" or event.get("state") == "hidden"
            if leaving and away_since is None:
                away_since = t
            elif not leaving and away_since is not None:
                summary.away_seconds += (t - away_since) / 1000
                summary.away_count += 1
                away_since = None
        elif kind == "focus" and away_since is not None:
            summary.away_seconds += (t - away_since) / 1000
            summary.away_count += 1
            away_since = None

    if away_since is not None:
        # 戻らないまま記録が終わった（画面を閉じた）。終わりまでを数える。
        summary.away_seconds += (last_t - away_since) / 1000
        summary.away_count += 1
    summary.duration_seconds = (last_t - (first_t or 0.0)) / 1000
    return summary


def active_tabs(events: Iterable[dict[str, Any]]) -> list[int]:
    """各イベントの時点で学習者が開いていたタブ（イベントと同じ並び）。

    `tab` を持つ操作（打鍵・貼り付け・実行など）はそのタブ。持たない出来事
    （画面を離れた・戻った・ハートビート）は、直前の `tab` の切り替えで決まる
    開いていたタブに振り分ける。記録の始まりは最初のタブ（0）。
    """
    current = 0
    result: list[int] = []
    for event in events:
        if event.get("type") == "tab" and isinstance(event.get("to"), int):
            current = int(event["to"])
            result.append(current)
            continue
        tab = event.get("tab")
        result.append(tab if isinstance(tab, int) else current)
    return result


def summarize_by_tab(events: Iterable[dict[str, Any]]) -> dict[int, ActivitySummary]:
    """問題（タブ）ごとの要約。**教員が知りたいのは「この問題をどう書いたか」**。

    1 回のセッションで複数の問題を行き来するので、セッション全体の合計では
    問題ごとの様子が読めない。各イベントを `active_tabs` で振り分けて数える。
    """
    ordered = list(events)
    by_tab: dict[int, list[dict[str, Any]]] = {}
    for event, tab in zip(ordered, active_tabs(ordered), strict=True):
        by_tab.setdefault(tab, []).append(event)
    return {tab: summarize(tab_events) for tab, tab_events in sorted(by_tab.items())}
