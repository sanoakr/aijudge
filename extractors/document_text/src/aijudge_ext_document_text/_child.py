"""文書の解析を行う子プロセス（#420）。

標準入力で文書を受け取り、標準出力に JSON（`{"text": ...}` か
`{"error": ...}`）を返す。**メモリの上限は自分で掛ける** ── 親の
`preexec_fn` はスレッドのあるプロセス（採点ワーカーはリースを延ばす
スレッドを持つ）では安全でない。
"""

from __future__ import annotations

import contextlib
import json
import resource
import sys

from aijudge_core import ArtifactKind


def main() -> None:
    kind = ArtifactKind(sys.argv[1])
    memory = int(sys.argv[2])
    # macOS は RLIMIT_AS を受け付けない。時間の上限は親が掛けている。
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    from . import DocumentTextError, _parse_in_process

    payload = sys.stdin.buffer.read()
    try:
        result = {"text": _parse_in_process(kind, payload)}
    except DocumentTextError as exc:
        result = {"error": str(exc)}
    except MemoryError:
        result = {"error": "解析中にメモリの上限に達しました"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
