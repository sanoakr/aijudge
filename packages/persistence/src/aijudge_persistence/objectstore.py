"""オブジェクトストレージ上の Artifact ストア（MinIO / S3 互換）。

個人情報を含む提出物を外部に出さないため、想定は**学内の MinIO**。
API が S3 互換なので boto3 で話す。`boto3` は任意依存で、入っていなければ
使おうとした時点で分かるように失敗させる（黙ってファイルストアに落ちない。
落ちると本番で提出物がワーカーのローカルディスクに散る）。
"""

from __future__ import annotations

import os

from aijudge_submission.protocols import SubmissionStoreError

ENV_ENDPOINT = "AIJUDGE_S3_ENDPOINT"
ENV_BUCKET = "AIJUDGE_S3_BUCKET"
ENV_ACCESS_KEY = "AIJUDGE_S3_ACCESS_KEY"
ENV_SECRET_KEY = "AIJUDGE_S3_SECRET_KEY"


class ObjectArtifactStore:
    """S3 互換ストレージ。既定は環境変数から読む。"""

    def __init__(
        self,
        bucket: str | None = None,
        *,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        client: object | None = None,
    ) -> None:
        self.bucket = bucket or os.environ.get(ENV_BUCKET, "aijudge-artifacts")
        if client is not None:
            self._client = client
            return
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - 依存が無い環境
            raise SubmissionStoreError(
                "the object store needs boto3; install the 's3' extra "
                "(uv sync --extra s3) or use FilesystemArtifactStore"
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or os.environ.get(ENV_ENDPOINT),
            aws_access_key_id=access_key or os.environ.get(ENV_ACCESS_KEY),
            aws_secret_access_key=secret_key or os.environ.get(ENV_SECRET_KEY),
        )

    def put(self, key: str, payload: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=payload)  # type: ignore[attr-defined]

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)  # type: ignore[attr-defined]
        except Exception as exc:
            # 中身が無いのに空バイト列を返すと、内容の無い提出が採点される。
            raise SubmissionStoreError(f"no artifact stored at {key!r}: {exc}") from exc
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)  # type: ignore[attr-defined]
        except Exception:
            return False
        return True
