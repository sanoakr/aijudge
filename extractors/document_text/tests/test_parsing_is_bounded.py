"""文書の解析は子プロセスで、時間とメモリの上限付きで動く（#420）。

細工した PDF でワーカーを止めさせない。上限を超えたら「読めなかった」として
扱い、採点はほかの経路と同じく続く。
"""

from __future__ import annotations

import subprocess

import aijudge_ext_document_text as document_text
import pytest

from aijudge_core import ArtifactKind


def test_a_parse_that_runs_too_long_is_reported_not_waited_for(monkeypatch) -> None:
    def too_slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(document_text.subprocess, "run", too_slow)
    with pytest.raises(document_text.DocumentTextError, match="秒で終わりませんでした"):
        document_text.text_of(b"%PDF-1.4", ArtifactKind.PDF)


def test_a_child_killed_by_its_limit_is_reported(monkeypatch) -> None:
    def killed(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=-9, stdout=b"", stderr=b"")

    monkeypatch.setattr(document_text.subprocess, "run", killed)
    with pytest.raises(document_text.DocumentTextError, match="途中で止まりました"):
        document_text.text_of(b"%PDF-1.4", ArtifactKind.PDF)


def test_the_parse_really_happens_in_another_process(monkeypatch) -> None:
    """親の中で pypdf を呼ばない。呼べば上限が効かない。"""
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(argv, **kwargs):
        calls.append(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(document_text.subprocess, "run", spy)
    with pytest.raises(document_text.DocumentTextError):
        document_text.text_of(b"%not a pdf", ArtifactKind.PDF)
    assert calls and calls[0][2] == "aijudge_ext_document_text._child"
