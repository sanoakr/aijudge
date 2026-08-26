"""aiJudge authoring (S2) — 課題・ルーブリック・テストケースの作成と取り込み。

AI 作問はこのパッケージのワーカーとして後から足す。
既存資産（Sharif Judge の課題ディレクトリ）の取り込みを先にするのは、
「教員が課題を作り直さずに新システムを試せる」ことが
PoC-0 を実運用に載せる条件だから。
"""

from __future__ import annotations

from .importers import sharif_judge

__all__ = ["sharif_judge"]
