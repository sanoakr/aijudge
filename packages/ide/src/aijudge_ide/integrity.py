"""行動記録の欠落と食い違いを数える（設計書 §6.3・§6.4・§6.8、ADR 0023）。

記録を**読む側**の計算である。受け口では解析しない（ADR 0023「やらないこと」）。

数えるのは 3 つ。どれも**判断を加えずに事実として**教員に示すためのもので、
不正の証拠にはしない ── 欠落は通信の不調と区別できない（ADR 0023 §2）。

- **束の欠落**: `seq` の飛び。送れなかった、または届かなかった束
- **沈黙**: イベントの時刻に、ハートビート（30 秒）が 3 回分以上ない区間。
  画面を閉じていた・PC が眠っていた・通信が切れていた、のどれか
- **組み立て直しの食い違い**: 「直前の全文 + 差分」で内容を組み立て直し、
  束の末尾の指紋（`tabs`）・実行・提出の指紋と比べる。一致しなければ、記録か
  内容のどちらかが改変された印になる（設計書 §6.4）。**欠落のあとは組み立て
  られない**ので、次の全文で組み立て直すまでは「確かめられない」として数える
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .activity import EventBatch

# ハートビートの間隔（画面側 `REC_HEARTBEAT_MS`）の 3 回分。これより長く何も
# 届かない区間を沈黙として数える。
SILENCE_MS = 90_000


@dataclass(frozen=True)
class Silence:
    """何も届かなかった区間（画面を開いてからの経過・ミリ秒）。"""

    start_ms: float
    end_ms: float

    @property
    def seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000


@dataclass
class IntegrityReport:
    batches: int = 0
    missing_seqs: list[int] = field(default_factory=list)
    silences: list[Silence] = field(default_factory=list)
    # 指紋と突き合わせた回数、一致した回数、食い違った回数、確かめられなかった回数。
    checks: int = 0
    matched: int = 0
    mismatched: int = 0
    unverifiable: int = 0
    first_mismatch_ms: float | None = None
    # 学習者の PC の時計のずれ（受信時刻 − 送信時の PC の時刻、秒の中央値）。
    # 正なら PC が遅れている。通信の遅れを含むので、目安である。
    clock_offset_seconds: float | None = None

    @property
    def complete(self) -> bool:
        """欠けも食い違いも無いか。"""
        return not self.missing_seqs and not self.mismatched and not self.unverifiable


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_session(
    batches: Sequence[tuple[EventBatch, Sequence[dict[str, Any]]]],
    snapshot: Callable[[str], str | None],
) -> IntegrityReport:
    """1 回の IDE のセッションを調べる。

    `batches` は (索引, そのイベント列) を seq の順に。`snapshot` は指紋から全文を
    引く（無ければ None）。
    """
    report = IntegrityReport(batches=len(batches))
    texts: dict[int, str | None] = {}
    expected = 0
    last_t: float | None = None
    offsets: list[float] = []

    def verify(tab: int, digest: str | None) -> None:
        """いまの組み立てを指紋と比べる。確かめられなければ全文で組み立て直す。"""
        if not digest:
            return
        report.checks += 1
        current = texts.get(tab)
        if current is None:
            report.unverifiable += 1
        elif _hash(current) == digest:
            report.matched += 1
            return
        else:
            report.mismatched += 1
            if report.first_mismatch_ms is None:
                report.first_mismatch_ms = last_t
        # 全文があれば、そこから組み立て直す（次の区間は確かめられる）。
        texts[tab] = snapshot(digest)

    for batch, events in batches:
        if batch.client_time is not None:
            offsets.append((batch.received_at - batch.client_time).total_seconds())
        if batch.seq > expected:
            report.missing_seqs.extend(range(expected, batch.seq))
            # 欠けた束の差分が無いので、どのタブも組み立てられなくなる。
            texts = dict.fromkeys(texts)
        expected = max(expected, batch.seq + 1)

        for event in events:
            t = float(event.get("t", 0))
            if last_t is not None and t - last_t >= SILENCE_MS:
                report.silences.append(Silence(last_t, t))
            last_t = t if last_t is None else max(last_t, t)
            kind = event.get("type")
            tab = event.get("tab")

            if kind == "hello":
                for index, digest in enumerate(event.get("hashes") or []):
                    texts[index] = snapshot(digest) if digest else None
            elif kind == "edit" and isinstance(tab, int):
                current = texts.get(tab)
                if current is None:
                    continue
                offset = int(event.get("off", 0))
                removed = int(event.get("del", 0))
                if offset < 0 or offset + removed > len(current):
                    # 範囲外の差分。組み立てられないので、確かめられない扱いにする。
                    texts[tab] = None
                    continue
                texts[tab] = (
                    current[:offset] + str(event.get("ins", "")) + current[offset + removed :]
                )
            elif kind == "file_load" and isinstance(tab, int):
                # 読み込みは差分ではなく全文の置き換え。全文から組み立て直す。
                digest = event.get("hash")
                texts[tab] = snapshot(digest) if digest else None
            elif kind in ("run", "submit") and isinstance(tab, int):
                if kind == "run" and event.get("stage") != "request":
                    continue
                verify(tab, event.get("hash"))
            elif kind == "tabs":
                for index, digest in enumerate(event.get("hashes") or []):
                    verify(index, digest)

    if offsets:
        report.clock_offset_seconds = statistics.median(offsets)
    return report
