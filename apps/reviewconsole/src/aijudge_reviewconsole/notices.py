"""保存のあとに一度だけ出す知らせ（段階 4 の片付け・計画書 §5）。

教員が押した操作の結果（足した課題・確定した件数・片付けた内訳・採点へ回した件数・
知識要素の足す外す・テストケース生成の失敗・再採点の件数）を、**戻った先の画面に
一度だけ**出すためにある。

以前は `Console.last_*` の 8 つの属性で、**コース単位**に持ち、**読んでも消さな
かった**（知識要素だけは消していた）。そのため、同じコースの別の教員が画面を開くと
他人の操作の知らせが出て、自分でも開き直すたびに同じ知らせが出続けた
（2026-09-28 に見直し、「操作した本人に一度だけ」と決めた）。

- **（利用者, コース, 種類, 範囲）ごとに 1 つ。** 範囲は課題ごとの知らせ
  （テストケース生成の失敗・再採点）で課題 ID を入れる ── 入れないと、別の課題の
  画面を先に開いたときにそこで消費される
- **取り出したら消える**（`take`）
- 置き場所はプロセスのメモリ（以前と同じ）。再起動で消えてよい ── 知らせは
  操作の直後に 1 回読むもので、記録ではない（記録は監査・ADR 0016）
- **溜め込まない。** 読まれなかった知らせ（戻る前に閉じた等）が残り続けないよう、
  上限を超えたら古いものから捨てる
"""

from __future__ import annotations

import threading
from collections import OrderedDict

# 種類。**文字列を書き写さない** ── 書き手と読み手で綴りが食い違うと、黙って出ない。
TASK_SAVED = "task_saved"
FINALIZED = "finalized"
UNIT_CLEARED = "unit_cleared"
JOBS_RELEASED = "jobs_released"
KC_SCOPE = "kc_scope"
TEST_CASE_ERROR = "test_case_error"
REGRADED = "regraded"
# どの観点も使っていない検証データ（入出力・参照解答）を、保存のときに外した（課題ごと）
PRUNED = "pruned"

# 読まれずに残る知らせの上限。利用者 × コース × 種類で、1 学期の運用なら
# 数百に届かない。超えるのは読まれずに溜まったときだけなので、古いものから捨てる。
MAX_NOTICES = 1000

_Key = tuple[str, str, str, str]


class Notices:
    """一度だけ出す知らせの置き場所。`Console.notices` に 1 つ持つ。"""

    def __init__(self, *, limit: int = MAX_NOTICES) -> None:
        self._items: OrderedDict[_Key, object] = OrderedDict()
        self._limit = limit
        # 同期のルートはスレッドで走る（FastAPI）。辞書の出し入れを 1 つずつにする。
        self._lock = threading.Lock()

    def put(
        self, user_id: object, course_id: object, kind: str, value: object, *, scope: object = ""
    ) -> None:
        """知らせを置く。同じ鍵の前の知らせは上書きする（最新の操作だけを言う）。"""
        key = (str(user_id), str(course_id), kind, str(scope))
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = value
            while len(self._items) > self._limit:
                self._items.popitem(last=False)

    def take(
        self, user_id: object, course_id: object, kind: str, *, scope: object = ""
    ) -> object | None:
        """知らせを取り出す。**取り出したら消える**（無ければ None）。"""
        key = (str(user_id), str(course_id), kind, str(scope))
        with self._lock:
            return self._items.pop(key, None)


__all__ = [
    "FINALIZED",
    "JOBS_RELEASED",
    "KC_SCOPE",
    "MAX_NOTICES",
    "PRUNED",
    "REGRADED",
    "TASK_SAVED",
    "TEST_CASE_ERROR",
    "UNIT_CLEARED",
    "Notices",
]
