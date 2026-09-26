"""提出された動画の配信。"""

from __future__ import annotations

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from aijudge_core import content_disposition, content_type_for
from aijudge_submission import StreamingArtifactStore, iter_file, parse_range


def serve_video(
    store: StreamingArtifactStore | None, request: Request, artifact: object, artifact_id: str
) -> Response:
    """動画を Range 対応でストリーム配信する。**全体をメモリに読まない。**

    学習者は自分の提出を見返し、教員は視聴して採点する ── どちらも同じ応答を返す。
    store が無い構成（動画を受け付けない運用）は、提出物が無いのと同じ 404 にする。
    """
    if store is None:
        raise HTTPException(status_code=404, detail="提出物が見つかりません")
    key = artifact.storage_key  # type: ignore[attr-defined]
    filename = artifact.filename  # type: ignore[attr-defined]
    try:
        size = store.size(key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="提出物が見つかりません") from exc
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition("inline", filename, artifact_id),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    media_type = content_type_for(artifact.filename)  # type: ignore[attr-defined]
    span = parse_range(request.headers.get("range"), size)
    if span is None:
        return StreamingResponse(
            iter_file(store.open_read(key)),
            media_type=media_type,
            headers={**headers, "Content-Length": str(size)},
        )
    start, end = span
    return StreamingResponse(
        iter_file(store.open_read(key), start=start, length=end - start + 1),
        status_code=206,
        media_type=media_type,
        headers={
            **headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        },
    )
