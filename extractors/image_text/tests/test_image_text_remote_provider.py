"""提出画像を学外のプロバイダへ送らないことを固定する（P7・#690）。

ゲートウェイの P7 検査は呼び出し側が申告した機微度しか見ない。この抽出器が
`DataClass.PERSONAL` を `NON_PERSONAL` に取り違えると、学外の vision モデルが
設定されたとき提出画像がそのまま送られる。既定の `ScriptedProvider` は
`local=True` なので、学外を模さないと取り違えが見えない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aijudge_ext_image_text import ImageText

from aijudge_core import Artifact, ArtifactKind, ArtifactRole
from aijudge_core.ids import ArtifactId, SubmissionId
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def _artifact() -> Artifact:
    return Artifact(
        id=ArtifactId("art_" + "9" * 32),
        submission_id=SubmissionId("sub_" + "8" * 32),
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.IMAGE,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(IMAGE_BYTES),
        filename="答案.png",
        created_at=NOW,
    )


def test_the_image_is_never_sent_to_a_remote_provider() -> None:
    provider = ScriptedProvider(
        [json.dumps({"readable": True, "lines": ["認定証"]}, ensure_ascii=False)],
        vision=True,
        local=False,
    )
    out = ImageText(LlmGateway(provider), model="vl").extract(_artifact(), IMAGE_BYTES)

    assert provider.calls == [], "提出画像が学外プロバイダに送られた"
    assert not out.succeeded
    assert out.failed_reason is not None and "PolicyViolation" in out.failed_reason
