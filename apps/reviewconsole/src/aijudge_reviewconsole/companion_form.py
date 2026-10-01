"""クライアント・サーバのケースの編集欄を読む（2026-10-01）。

画面（`_companion_edit_block.html`）が送る欄から、`network_test_runner` のケースを
組み直す。**検査は評価器と同じ条件**にする ── 役割は client / server、ポートは
1024〜65535 の整数、伴走ソースは空でない（`aijudge_eval_network_test_runner` の
`_run_case` が断る値を、保存の時点で断る。採点まで気づかないと、その課題の提出が
全部「採点できず」になる）。

**画面に出していない値は捨てない。** 付属ファイル（`fixtures`）などは元のケース
（`case_orig` の名前）から引き継ぐ。
"""

from __future__ import annotations

from collections.abc import Mapping

from aijudge_authoring.spec import TestCaseSpec
from aijudge_core import TaskVersion

# 評価器が受け付ける役割とポートの範囲（`network_test_runner._run_case` と同じ）。
ROLES = ("client", "server")
MIN_PORT = 1024
MAX_PORT = 65535


class CompanionFormError(ValueError):
    """欄の値が評価器の受け付けない形。文面は画面にそのまま出す。"""


def _lines(text: str) -> list[str]:
    """1 行 1 つの断片。空行は捨てる（行末の空白は断片の一部なので残す）。"""
    return [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def _text(text: str) -> str:
    return text.replace("\r\n", "\n")


def companion_cases_from_form(
    form, version: TaskVersion, *, evaluator_id: str
) -> tuple[TestCaseSpec, ...]:
    """欄の値からケースを組む。0 件も許す（観点は採点できず、総点は伏せられる）。"""
    getlist = form.getlist
    sources: dict[str, str] = {}
    for name, body in zip(getlist("src_name"), getlist("src_body"), strict=False):
        name, body = str(name).strip(), _text(str(body))
        if not name and not body.strip():
            continue  # 追加用の空欄
        if not name:
            raise CompanionFormError("伴走プロセスのソースにファイル名を付けてください")
        if not body.strip():
            raise CompanionFormError(f"伴走プロセスのソース {name} が空です")
        if name in sources:
            raise CompanionFormError(f"伴走プロセスのソース {name} が 2 つあります")
        sources[name] = body

    originals: Mapping[str, Mapping[str, object]] = {
        case.name: case.payload for case in version.test_cases if case.evaluator_id == evaluator_id
    }
    columns = {
        key: [str(value) for value in getlist(key)]
        for key in (
            "case_orig",
            "case_name",
            "case_role",
            "case_port",
            "case_companion",
            "case_input",
            "case_expected",
            "case_companion_input",
            "case_companion_expected",
            "case_hidden",
            "case_weight",
        )
    }
    deleted = {str(value) for value in getlist("case_delete")}

    def at(key: str, index: int, default: str = "") -> str:
        values = columns[key]
        return values[index] if index < len(values) else default

    cases: list[TestCaseSpec] = []
    seen: set[str] = set()
    for index in range(len(columns["case_name"])):
        if str(index) in deleted:
            continue
        name = at("case_name", index).strip()
        if not name:
            continue  # 追加用の空行
        if name in seen:
            raise CompanionFormError(f"ケース {name!r} が重複しています")
        seen.add(name)
        role = at("case_role", index)
        if role not in ROLES:
            raise CompanionFormError(f"{name}: 提出物の役割は client か server です")
        try:
            port = int(at("case_port", index))
        except ValueError:
            raise CompanionFormError(f"{name}: ポートが整数ではありません") from None
        if not MIN_PORT <= port <= MAX_PORT:
            raise CompanionFormError(f"{name}: ポートは {MIN_PORT}〜{MAX_PORT} にしてください")
        companion_name = at("case_companion", index).strip()
        if companion_name not in sources:
            raise CompanionFormError(
                f"{name}: 伴走プロセスのソース {companion_name!r} がありません"
            )
        try:
            weight = float(at("case_weight", index, "1.0") or 1.0)
        except ValueError:
            raise CompanionFormError(f"{name}: 重みが数値ではありません") from None
        if weight <= 0:
            raise CompanionFormError(f"{name}: 重みは正の値にしてください")

        # 元のケースの値を土台にする（画面に出していない値を捨てない）。
        payload = dict(originals.get(at("case_orig", index).strip(), {}))
        payload.update(
            {
                "role": role,
                "port": port,
                "companion_name": companion_name,
                "companion": sources[companion_name],
                "input": _text(at("case_input", index)),
                "companion_input": _text(at("case_companion_input", index)),
                "expected_contains": _lines(at("case_expected", index)),
                "companion_expected_contains": _lines(at("case_companion_expected", index)),
            }
        )
        cases.append(
            TestCaseSpec(
                name=name,
                # **この 1 件を読む評価器を明示する**（#402）。課題の既定に倒すと
                # `code_test_runner` あてになり、ポートも伴走ソースも消える。
                evaluator=evaluator_id,
                payload=payload,
                hidden=at("case_hidden", index, "1") == "1",
                weight=weight,
            )
        )
    return tuple(cases)


__all__ = ["CompanionFormError", "companion_cases_from_form"]
