"""2 つの Web アプリが共有する部品（段階的な立て直し 1-2、#437）。

以前は学習者アプリ（`apps/studentweb`）と教員コンソール（`apps/reviewconsole`）が
同じ関数をそれぞれの `app.py` に 1 部ずつ持っていた。一字一句同じまま 2 部あると、
片方だけ直した日に気づかないまま振る舞いが分かれる ── `current_principal` は
実際にそうなっている（コンソールだけが要求の中でキャッシュする）。

**ここに置くのは、両アプリで振る舞いが完全に同じものだけ。** 違いがあるものは
違いを確かめてから別の PR で寄せる（`docs/design/staged-refactor.md` §3 段階 1-2）。

`aijudge_webui` と分けているのは、あちらが「ファイルとパスだけ、依存なし」という
約束だから（#184）。ここは HTTP の要求と応答を扱うので fastapi に依存する。
"""

from __future__ import annotations

from .footer import read_app_version, read_copyright_notice
from .urls import counterpart_url
from .video import serve_video

__all__ = [
    "counterpart_url",
    "read_app_version",
    "read_copyright_notice",
    "serve_video",
]
