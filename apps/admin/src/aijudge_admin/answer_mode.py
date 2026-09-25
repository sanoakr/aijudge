"""問題セットの答え方を決めてよいか（ADR 0026・設計書 §4.1）。

**エディタにできるのは、エディタで書ける課題（提出形式に `.c`・`.py`・`.md` の
どれか）を 1 つ以上含む問題セット**（2026-09-25 改訂）。以前は**全課題**が書ける
形式を持つことを求めていたが、画像や PDF を出す課題はエディタの画面からファイルを
選んで出せるようになった（`aijudge_ide.attachable_suffixes`）。書ける課題が 1 つも
無いセット（画像・PDF・動画だけ）はエディタにしない ── 作業の記録もエディタの
利点も無く、課題の画面から出すのと変わらない。

**動画を受ける課題があるセットでは、ファイル選択での提出を止められない。** 動画は
エディタの画面から出せない（専用の分割送信の経路しか無い）ので、止めると動画を
出す道が無くなる。

`.md` だけの課題（オンラインのレポート試験）もエディタにできる。**実行でき
るかは別の問い**で、プログラムの形式が採点の言語と一致するときだけ画面に
「実行」が出る（runner がもう一度確かめる）。

規則をここに置くのは、画面と API と定義の流し込み（`course apply`）の全部から
同じ検査を通すため。**画面で灰色にするだけでは境界にならない**（#146）──
保存する側がもう一度確かめる。
"""

from __future__ import annotations

from collections.abc import Sequence

from aijudge_core import Course, Task, TaskVersion, allowed_suffixes
from aijudge_ide import EDITOR_FORMATS, editor_formats, video_suffixes


def editor_blockers(tasks: Sequence[tuple[Task, TaskVersion]], course: Course) -> tuple[str, ...]:
    """エディタにできない理由。空ならできる。

    提出形式は画面と受付と同じ関数（`allowed_suffixes`：課題の指定、空なら
    コースの既定）で決める。別の決め方をすると、画面がエディタを出したのに
    提出で断られる。
    """
    if not tasks:
        return ()
    if any(
        editor_formats(allowed_suffixes(task.accepted_suffixes, course.upload_suffixes))
        for task, _version in tasks
    ):
        return ()
    wanted = "・".join(EDITOR_FORMATS)
    return (
        f"エディタで書ける課題がありません（提出形式に {wanted} のどれかを含む課題が 1 つも無い）",
    )


def file_upload_required(
    tasks: Sequence[tuple[Task, TaskVersion]], course: Course
) -> tuple[str, ...]:
    """ファイル選択での提出を止められない理由（動画を受ける課題）。空なら止められる。"""
    reasons: list[str] = []
    for task, _version in tasks:
        videos = video_suffixes(allowed_suffixes(task.accepted_suffixes, course.upload_suffixes))
        if videos:
            # 教員が読む文言なので、課題の名前で指す（ID では読めない）。
            reasons.append(
                f"{task.title}: 動画（{'・'.join(videos)}）はエディタの画面から出せないため、"
                "ファイル選択での提出が要ります"
            )
    return tuple(reasons)
