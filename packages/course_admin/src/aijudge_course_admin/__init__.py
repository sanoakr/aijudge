"""コース運営の業務処理（段階的な立て直し 段階 3）。

`apps/admin` は CLI と業務処理が同じ場所にあり、コンソール（`reviewconsole`）は
業務処理を使うためにアプリを import していた（アプリ間の依存・#438）。業務処理を
ここへ葉から順に移し、`apps/admin` は CLI だけにする。

移す間は旧い場所に再エクスポートを残す（計画 §1 の 4）。呼び出し側を張り替え終えた
ところで消す（段階 3-6）。
"""

from __future__ import annotations

from .errors import AdminError

__all__ = ["AdminError"]
