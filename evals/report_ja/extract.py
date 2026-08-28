"""19 件のレポートを本文に直して置く。**採点はしない。**

以降の反復（ルーブリックを直して測り直す）で毎回 PDF を開き直さないための
下ごしらえ。抽出そのものは normalizers/document_text がやる ── ここで
別の抽出を書くと、採点が見る本文と測定が見る本文がずれる。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "dev/aijudge"))

from datetime import UTC, datetime

from aijudge_norm_document_text import DocumentText

from aijudge_core import Artifact, ArtifactKind, ArtifactRole
from aijudge_core.ids import ArtifactId, SubmissionId, new_id

REPORTS = Path.home() / "pCloud Drive/Agent Projects/aiJudge設計検討/network-report2025"
CSV = Path.home() / "pCloud Drive/Agent Projects/aiJudge設計検討/network2025-report.csv"
OUT = Path(__file__).parent / "bodies"

# ファイル名から学籍番号を取る。`Y230035_HTTPサーバ性能評価...pdf` のように
# 後ろに題名が付くものがあるので、先頭の学籍番号だけを見る。
STUDENT_RE = re.compile(r"^([Yy]\d{6})")


def main() -> int:
    graded = {row["学籍番号"].upper(): row for row in csv.DictReader(CSV.open(encoding="utf-8"))}
    normalizer = DocumentText()
    index = []

    for path in sorted(REPORTS.iterdir()):
        match = STUDENT_RE.match(path.name)
        if match is None:
            continue
        login = match.group(1).upper()
        kind = ArtifactKind.PDF if path.suffix.lower() == ".pdf" else ArtifactKind.DOCX
        payload = path.read_bytes()
        artifact = Artifact(
            id=ArtifactId(new_id("art")),
            submission_id=SubmissionId(new_id("sub")),
            filename=path.name,
            kind=kind,
            role=ArtifactRole.ORIGINAL,
            storage_key=path.name,
            content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
            created_at=datetime.now(UTC),
        )
        body = normalizer.normalize(artifact, payload)
        try:
            text = body.decode("utf-8")
            readable = True
        except UnicodeDecodeError:
            text = ""
            readable = False

        (OUT / f"{login}.txt").write_text(text, encoding="utf-8")
        row = graded.get(login)
        index.append(
            {
                "login": login,
                "filename": path.name,
                "kind": kind.value,
                "readable": readable,
                "characters": len(re.sub(r"\s", "", text)),
                "human": None
                if row is None
                else {
                    "A_format": int(row["A_体裁4"]),
                    "B_target": int(row["B_実験先3"]),
                    "C_conditions": int(row["C_条件2"]),
                    "D_originality": int(row["D_独自性8"]),
                    "E_discussion": int(row["E_考察4"]),
                    "F_results": int(row["F_結果2"]),
                    "raw_total": int(row["計算点(23)"]),
                    "normalized20": float(row["正規化20点"]),
                    "final": float(row["最終点"]),
                    "comment": row["コメント"],
                },
            }
        )

    (Path(__file__).parent / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    missing = [r["login"] for r in index if r["human"] is None]
    print(f"抽出 {len(index)} 件 / 教員採点あり {len(index) - len(missing)} 件")
    if missing:
        print(f"  教員採点が見つからない: {missing}")
    for r in index:
        flag = "" if r["readable"] else "  ← 読めない"
        print(f"  {r['login']:8} {r['kind']:5} {r['characters']:6} 字{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
