"""クライアント・サーバの課題の検証データを、画面に出す形にする（2026-10-01）。

`network_test_runner` の検証データ（伴走プロセスのソース・役割・ポート・入力・
期待する断片・付属ファイル）は、すべてケースの `payload` に入っている。以前は
画面のどこにも出ておらず、教員も TA も中身を確かめられなかった（course.yaml を
読むしかなかった）。

**伴走プロセスのソースはケースごとに写しが入っている**（取り込みが各ケースに同じ
ものを入れる・`companion.py`）。そのまま並べると同じソースが何度も出るので、
ファイル名と中身で 1 つにまとめ、ケースからはその名前で指す。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_core import TestCase


@dataclass(frozen=True)
class CompanionSource:
    name: str
    source: str


@dataclass(frozen=True)
class CompanionCase:
    name: str
    hidden: bool
    weight: float
    role: str
    port: object
    input: str
    companion_input: str
    expected_contains: tuple[str, ...]
    companion_expected_contains: tuple[str, ...]
    companion_name: str
    fixtures: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CompanionView:
    sources: tuple[CompanionSource, ...]
    cases: tuple[CompanionCase, ...]


def companion_view(cases: tuple[TestCase, ...] | list[TestCase]) -> CompanionView:
    """ケースを、まとめた伴走ソースとケースごとの行に分ける。"""
    sources: dict[tuple[str, str], CompanionSource] = {}
    rows: list[CompanionCase] = []
    for case in cases:
        payload = dict(case.payload)
        name = str(payload.get("companion_name") or "companion.py")
        source = str(payload.get("companion") or "")
        if source:
            sources.setdefault((name, source), CompanionSource(name=name, source=source))
        fixtures = payload.get("fixtures")
        rows.append(
            CompanionCase(
                name=case.name,
                hidden=case.hidden,
                weight=case.weight,
                role=str(payload.get("role") or ""),
                port=payload.get("port"),
                input=str(payload.get("input") or ""),
                companion_input=str(payload.get("companion_input") or ""),
                expected_contains=_strings(payload.get("expected_contains")),
                companion_expected_contains=_strings(payload.get("companion_expected_contains")),
                companion_name=name,
                fixtures=tuple((str(key), str(value)) for key, value in sorted(fixtures.items()))
                if isinstance(fixtures, dict)
                else (),
            )
        )
    return CompanionView(sources=tuple(sources.values()), cases=tuple(rows))


def _strings(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


__all__ = ["CompanionCase", "CompanionSource", "CompanionView", "companion_view"]
