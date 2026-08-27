"""aiJudge student web — 学習者向け Web アプリ（合成の中心）。

提出して、結果を見る。それだけ。

**学習者に見せる範囲は `visibility.py` が決める。** 決定的評価の結果は
すぐ、AI の判定は教員が確定させてから（設計原則 P5）。テンプレートの
条件分岐に散らすと、画面を足したときに漏れる。
"""

from __future__ import annotations

from .app import SESSION_COOKIE, SUFFIX_KINDS, StudentApp, create_app
from .visibility import CriterionView, ResultView, build_result_view

__all__ = [
    "SESSION_COOKIE",
    "SUFFIX_KINDS",
    "CriterionView",
    "ResultView",
    "StudentApp",
    "build_result_view",
    "create_app",
]
