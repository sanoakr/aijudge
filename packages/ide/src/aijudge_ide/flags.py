"""行動記録に印を付ける（設計書 §7・ADR 0023 §4）。**判定はしない。**

ここが出すのは「経過を確かめる価値のある場所」の目印だけである。印が付いた
ことは不正を意味しない ── 教科書の例を貼る、自分の手元のファイルから貼る、
補完の無い環境で速く打てる人もいる。**誤検知の不利益が大きすぎる**ので、
自動で減点しない・成績につながない（`grading-does-not-know-ide`・
`grader-does-not-read-activity`）。教員は印から再生に飛び、自分の目で読む。

閾値はどれも「人がふつうに書いていれば、まず越えない」側に置いた目安である。
学期の記録が溜まったら、実際の分布を見て直すこと（ここには根拠の実測が無い）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# 大きいと見なす外部からの貼り付け（文字数）。関数 1 つ・数行を越える量。
LARGE_PASTE_CHARS = 200
# 「戻った直後」と見なす時間（ミリ秒）。画面を離れて何かを見てきた、の目安。
AFTER_RETURN_MS = 30_000
# 問題文をコピーしてから、戻って貼るまでを 1 つの流れと見なす時間（ミリ秒）。
STATEMENT_ROUNDTRIP_MS = 10 * 60_000
# 流れの最後の貼り付けとして見る大きさ（文字数）。
ROUNDTRIP_PASTE_CHARS = 80
# 貼り付けを伴わない 1 回の挿入として大きいもの（文字数）。自動入力のツールや
# ドラッグ＆ドロップは、貼り付けイベントを経ずに文字を流し込む。
BULK_INSERT_CHARS = 80
# 1 回の挿入に対応する貼り付け・読み込み・補完を探す前後の幅（ミリ秒）。画面は
# 貼り付けの記録を指紋の計算のあとに出すので、挿入より少し遅れて届く。
PAIRING_MS = 2_000
# 打鍵の速さを測る窓（ミリ秒）と、その窓で人には難しい文字数。1 秒に 15 字を
# 10 秒続ける ── 速い人でも 1 秒 8〜10 字程度である。
TYPING_WINDOW_MS = 10_000
TYPING_CHARS_IN_WINDOW = 150


class FlagKind(StrEnum):
    LARGE_EXTERNAL_PASTE = "large_external_paste"
    PASTE_AFTER_RETURN = "paste_after_return"
    STATEMENT_ROUNDTRIP = "statement_roundtrip"
    BULK_INSERT = "bulk_insert"
    FAST_TYPING = "fast_typing"
    SUBMISSION_MISMATCH = "submission_mismatch"


# 教員に見せる言葉。**「不正」「疑い」と書かない** ── 事実だけを言う。
FLAG_LABELS: dict[FlagKind, str] = {
    FlagKind.LARGE_EXTERNAL_PASTE: "大きな外からの貼り付け",
    FlagKind.PASTE_AFTER_RETURN: "画面に戻った直後の大きな貼り付け",
    FlagKind.STATEMENT_ROUNDTRIP: "問題文をコピー → 画面を離れる → 戻って貼り付け",
    FlagKind.BULK_INSERT: "貼り付けを伴わない一括の挿入",
    FlagKind.FAST_TYPING: "とても速い打鍵が続いた",
    FlagKind.SUBMISSION_MISMATCH: "提出した内容が記録と一致しない",
}


@dataclass(frozen=True)
class Flag:
    kind: FlagKind
    # 画面を開いてからの経過（ミリ秒）。再生はここへ飛ぶ。
    t: float
    detail: str

    @property
    def label(self) -> str:
        return FLAG_LABELS[self.kind]


def flag_events(events: Iterable[dict[str, Any]]) -> list[Flag]:
    """1 回のセッションのイベント列（時刻順）に印を付ける。"""
    ordered = list(events)
    flags: list[Flag] = []
    returned_at: float | None = None
    away = False
    statement_copy_at: float | None = None
    left_after_copy = False
    # 挿入と組になる出来事（貼り付け・読み込み・補完）の時刻。
    paired_times = [
        float(e.get("t", 0)) for e in ordered if e.get("type") in ("paste", "file_load", "suggest")
    ]
    typing: list[tuple[float, int]] = []
    typing_flagged_until = -1.0

    for event in ordered:
        kind = event.get("type")
        t = float(event.get("t", 0))

        if kind in ("blur",) or (kind == "visibility" and event.get("state") == "hidden"):
            away = True
            if statement_copy_at is not None:
                left_after_copy = True
        elif kind == "focus" or (kind == "visibility" and event.get("state") == "visible"):
            if away:
                returned_at = t
            away = False
        elif kind == "copy" and event.get("from") == "statement":
            statement_copy_at = t
            left_after_copy = False

        elif kind == "paste" and event.get("origin") != "internal":
            length = int(event.get("len", 0))
            after_return = returned_at is not None and 0 <= t - returned_at <= AFTER_RETURN_MS
            if length >= LARGE_PASTE_CHARS:
                flags.append(
                    Flag(
                        FlagKind.PASTE_AFTER_RETURN
                        if after_return
                        else FlagKind.LARGE_EXTERNAL_PASTE,
                        t,
                        f"{length} 字"
                        + (
                            f"（戻って {round((t - returned_at) / 1000)} 秒後）"
                            if after_return and returned_at is not None
                            else ""
                        ),
                    )
                )
            if (
                statement_copy_at is not None
                and left_after_copy
                and length >= ROUNDTRIP_PASTE_CHARS
                and t - statement_copy_at <= STATEMENT_ROUNDTRIP_MS
            ):
                flags.append(
                    Flag(
                        FlagKind.STATEMENT_ROUNDTRIP,
                        t,
                        f"コピーから {round((t - statement_copy_at) / 1000)} 秒後に {length} 字",
                    )
                )
                statement_copy_at = None

        elif kind == "edit" and not event.get("undo") and not event.get("redo"):
            inserted = len(str(event.get("ins", "")))
            if inserted >= BULK_INSERT_CHARS and not any(
                abs(p - t) <= PAIRING_MS for p in paired_times
            ):
                flags.append(Flag(FlagKind.BULK_INSERT, t, f"{inserted} 字"))
            elif inserted == 1:
                # 1 字ずつの打鍵だけを速さに数える（改行時の字下げ・括弧の自動
                # 補完は 1 字を超えるので入らない）。
                typing.append((t, 1))
                while typing and t - typing[0][0] > TYPING_WINDOW_MS:
                    typing.pop(0)
                if len(typing) >= TYPING_CHARS_IN_WINDOW and t > typing_flagged_until:
                    flags.append(
                        Flag(
                            FlagKind.FAST_TYPING,
                            typing[0][0],
                            f"{TYPING_WINDOW_MS // 1000} 秒で {len(typing)} 字",
                        )
                    )
                    # 同じ速い区間で何度も印を付けない。
                    typing_flagged_until = t + TYPING_WINDOW_MS
    return flags


def submission_mismatches(
    events: Iterable[dict[str, Any]], submitted_hashes: dict[str, str]
) -> list[Flag]:
    """画面が「提出した」と記録した内容と、実際の提出の内容が違う箇所。

    `submitted_hashes` は提出 ID → 提出の内容の指紋（`ide_submission_links`）。
    画面の記録は提出の直前に撮った全文の指紋を持つ。食い違えば、画面の外で
    提出が作られたか、記録が書き換えられた印になる。
    """
    flags: list[Flag] = []
    for event in events:
        if event.get("type") != "submit":
            continue
        submission_id = str(event.get("submission_id", ""))
        expected = submitted_hashes.get(submission_id)
        if expected is not None and event.get("hash") and event["hash"] != expected:
            flags.append(
                Flag(FlagKind.SUBMISSION_MISMATCH, float(event.get("t", 0)), submission_id)
            )
    return flags
