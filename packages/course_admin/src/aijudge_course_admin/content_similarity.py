"""提出どうしの**内容の近さ**を、埋め込みで測る（#569・ADR 0029 §7）。

Dolos は文字や字句の並びを比べるので、**言い換えを見つけない**（同じ内容を言い換えた組は 0）。
レポートでは、同じ内容を別の言葉で書いた組を見たい。そこで段落ごとに埋め込みを取り、
組ごとに「互いの段落がどれだけ近いか」を測る。**判定はしない**（P5）── 同じ課題のレポートは、
写していなくても内容が近い。だから課題ごとの組の**中央値**を残し、画面はそこからの差を示す。

- **学習者の文は手元のモデルにしか送らない**（P7）。`DataClass.PERSONAL` で `LlmGateway` を
  通すので、学外へ送る経路はポリシーが断る
- **モデルは 1 つに固定する。** 違うモデルの値どうしは比べられない。モデルは `run.json` の
  `image`（`embedding:<model>`）に残り、変われば入力の指紋が変わって測り直す
- 出力は Dolos の回と同じ形（`run.json` と `data/pairs.csv`）にする。入口のページ・後始末・
  自動実行が同じ口で扱える。根拠の段落は `data/passages.json`。どちらも学習者の文の写しで、
  提出と一緒に消し、バックアップしない（§4）
- 図表は、まずは本文として取り出せる文字（表のセル・軸の名前・図の題）だけが入る。図そのものの
  比較は実際のレポートで測ってから足す（2026-10-28 の決定で、この比べ方に統合する）

しきい値（`PARAGRAPH_CLOSE`）は、手元で作った 6 組の日本語の段落で決めた**仮の値**である
（qwen3-embedding:4b で、言い換えは 0.70〜0.90、同じ課題で独立に書いた段落は 0.45〜0.70）。
実際のレポートの提出が集まったら決め直す。`run.json` の `params` に残る。
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from aijudge_core import Task
from aijudge_llm_gateway import DataClass, LlmError, LlmGateway, default_gateway

from .code_similarity import (
    DATA_DIR,
    NotMeasuredReason,
    RunSubmission,
    SimilarityMode,
    SimilarityRun,
    _base,
    _Entry,
    _not_measured,
    _run_id,
    _write_run,
    task_dir,
)

logger = logging.getLogger(__name__)

#: 埋め込みのモデル。**未設定なら内容としては比べない**（記録も残さない）。
ENV_EMBEDDING_MODEL = "AIJUDGE_SIMILARITY_EMBEDDING_MODEL"
#: 根拠の段落を置くファイル。
PASSAGES_FILE = "passages.json"

#: これより短い段落は次とつなぐ。見出しや 1 行の箇条書きだけでは内容が測れない。
MIN_PARAGRAPH_CHARS = 80
#: これより長い段落は文の切れ目で分ける。PDF から取り出した本文は空行が無く、全体が 1 段落になる。
MAX_PARAGRAPH_CHARS = 800
#: 1 提出から使う段落の上限。組ごとの計算は段落の数の 2 乗で増える。
MAX_PARAGRAPHS = 60
#: 1 回の埋め込みの呼び出しで送る段落の数。
EMBED_BATCH = 16
EMBED_TIMEOUT_SECONDS = 300.0
#: 「近い段落」と数える近さ（仮の値・モジュールの docstring）。
PARAGRAPH_CLOSE = 0.75
#: 根拠の段落を残す組の数（近い順）。入口に出すのは上位 5 組だが、余裕を持たせる。
PASSAGE_PAIRS = 20
#: 1 組あたりに残す根拠の段落の組の数。
PASSAGES_PER_PAIR = 3

_SENTENCE_END = re.compile(r"(?<=[。．！？!?])")


@dataclass(frozen=True)
class ContentSettings:
    """内容として比べるのに要るもの。合成の中心（`aijudge_admin`）が作って渡す。"""

    gateway: LlmGateway
    model: str

    @property
    def tool(self) -> str:
        return f"embedding:{self.model}"

    @property
    def params(self) -> tuple[str, ...]:
        return (
            f"min_chars={MIN_PARAGRAPH_CHARS}",
            f"max_chars={MAX_PARAGRAPH_CHARS}",
            f"max_paragraphs={MAX_PARAGRAPHS}",
            f"close={PARAGRAPH_CLOSE}",
        )


def settings_from_env() -> ContentSettings | None:
    """運用の設定から作る。**モデルが未設定なら `None`**（内容としては比べない）。

    経路は採点と同じ `default_gateway()`（主系の PAIR、落ちていれば従系）。どちらにも同じ
    モデルを入れておく ── 片方にしか無いと、主系が落ちた周回だけ測れない
    （`EMBEDDING_UNAVAILABLE` で次の周回に回し直す）。
    """
    model = os.environ.get(ENV_EMBEDDING_MODEL, "").strip()
    if not model:
        return None
    return ContentSettings(gateway=default_gateway(), model=model)


def paragraphs(text: str) -> list[str]:
    """本文を段落に分ける。**短いものはつなぎ、長いものは文の切れ目で分ける。**"""
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", text)]
    merged: list[str] = []
    carry = ""
    for block in blocks:
        if not block:
            continue
        carry = f"{carry} {block}".strip() if carry else block
        if len(carry) >= MIN_PARAGRAPH_CHARS:
            merged.append(carry)
            carry = ""
    if carry:
        if merged and len(carry) < MIN_PARAGRAPH_CHARS:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    out: list[str] = []
    for block in merged:
        if len(block) <= MAX_PARAGRAPH_CHARS:
            out.append(block)
            continue
        piece = ""
        for sentence in _SENTENCE_END.split(block):
            if piece and len(piece) + len(sentence) > MAX_PARAGRAPH_CHARS:
                out.append(piece)
                piece = ""
            piece += sentence
        if piece:
            out.append(piece)
    return out[:MAX_PARAGRAPHS]


def _embed(settings: ContentSettings, texts: Sequence[str]) -> np.ndarray:
    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        vectors.extend(
            settings.gateway.embed(
                tuple(texts[start : start + EMBED_BATCH]),
                model=settings.model,
                # **学習者の文**（P7）。手元のモデルにしか送らない。
                data_class=DataClass.PERSONAL,
                timeout_seconds=EMBED_TIMEOUT_SECONDS,
            )
        )
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


@dataclass(frozen=True)
class _Pair:
    left: int
    right: int
    score: float
    close: list[tuple[float, int, int]]  # (近さ, 左の段落, 右の段落) ── 互いに最も近い組だけ


def _compare(left: np.ndarray, right: np.ndarray) -> tuple[float, list[tuple[float, int, int]]]:
    """2 提出の近さ。**互いの段落が、相手のどれかとどれだけ近いかの平均**（両側の平均）。

    1 段落だけ同じ組と、全体を言い換えた組を分けるために、最も近い 1 組ではなく平均を取る。
    根拠には、互いに最も近い段落の組のうち `PARAGRAPH_CLOSE` 以上のものを返す。
    """
    sims = left @ right.T
    best_right = sims.argmax(axis=1)
    best_left = sims.argmax(axis=0)
    score = float((sims.max(axis=1).mean() + sims.max(axis=0).mean()) / 2)
    close = [
        (float(sims[i, j]), i, int(j))
        for i, j in enumerate(best_right)
        if best_left[j] == i and sims[i, j] >= PARAGRAPH_CLOSE
    ]
    close.sort(reverse=True)
    return score, close


def measure(
    task: Task,
    entries: Sequence[_Entry],
    texts: Sequence[str],
    fingerprint: str,
    *,
    settings: ContentSettings,
    at: datetime,
    root: Path,
    skipped: int,
) -> SimilarityRun:
    """内容として 1 課題を測り、報告を残す。`texts` は `entries` と同じ並びの本文。"""
    mode = SimilarityMode.CONTENT

    def refuse(reason: NotMeasuredReason, detail: str, **extra: object) -> SimilarityRun:
        return _not_measured(
            task,
            fingerprint,
            reason,
            detail,
            mode=mode,
            tool=settings.tool,
            at=at,
            root=root,
            params=settings.params,
            **extra,
        )

    split = [paragraphs(text) for text in texts]
    started = time.monotonic()
    flat = [p for parts in split for p in parts]
    try:
        matrix = _embed(settings, flat)
    except LlmError as exc:
        return refuse(NotMeasuredReason.EMBEDDING_UNAVAILABLE, str(exc)[:300])
    blocks: list[np.ndarray] = []
    offset = 0
    for parts in split:
        blocks.append(matrix[offset : offset + len(parts)])
        offset += len(parts)

    pairs: list[_Pair] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            if not len(blocks[i]) or not len(blocks[j]):
                continue
            score, close = _compare(blocks[i], blocks[j])
            pairs.append(_Pair(i, j, score, close))
    pairs.sort(key=lambda p: (-p.score, -len(p.close)))
    duration = int((time.monotonic() - started) * 1000)

    target = task_dir(root, task.course_id, task.id) / _run_id(at, mode)
    data = target / DATA_DIR
    data.mkdir(parents=True, exist_ok=True)
    names = [entry.pseudonym for entry in entries]
    lines = ["id,leftFilePath,rightFilePath,similarity,longestFragment"]
    for index, pair in enumerate(pairs):
        lines.append(
            f"{index},{names[pair.left]}.txt,{names[pair.right]}.txt,"
            f"{pair.score:.4f},{len(pair.close)}"
        )
    (data / "pairs.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    passages = {
        f"{names[pair.left]}|{names[pair.right]}": [
            {
                "similarity": round(sim, 4),
                "left": split[pair.left][a],
                "right": split[pair.right][b],
            }
            for sim, a, b in pair.close[:PASSAGES_PER_PAIR]
        ]
        for pair in pairs[:PASSAGE_PAIRS]
    }
    (data / PASSAGES_FILE).write_text(json.dumps(passages, ensure_ascii=False), encoding="utf-8")

    detail = (
        f"読めなかった提出 {skipped} 件（文字が埋め込まれていない PDF など）" if skipped else ""
    )
    run = SimilarityRun.model_validate(
        _base(task, fingerprint, mode=mode, tool=settings.tool, at=at)
        | {
            "params": settings.params,
            "tool_version": settings.model,
            "submissions": tuple(
                RunSubmission(
                    submission_id=entry.submission.id,
                    pseudonym=entry.pseudonym,
                    filename=entry.filename,
                    content_hash=entry.content_hash,
                )
                for entry in entries
            ),
            "baseline": round(statistics.median(p.score for p in pairs), 4) if pairs else None,
            "detail": detail,
            "duration_ms": duration,
        }
    )
    _write_run(target, run)
    logger.info(
        "類似の検査: %s content %d 件・%d 段落 %.1f 秒（%s）",
        task.id,
        len(entries),
        len(flat),
        duration / 1000,
        settings.model,
    )
    return run
