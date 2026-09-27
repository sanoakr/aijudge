"""`aijudge_course_admin.kc` に移した（段階的な立て直し 3-2）。

旧い名前で import している呼び出し側のために、**同じモジュール**を指させる
（段階 3-6 で消す）。再エクスポートではなく `sys.modules` を差し替えるのは、
テストが旧い名前で属性を差し替えても（`monkeypatch`）新しい場所に効くようにするため。
"""

from __future__ import annotations

import sys

from aijudge_course_admin import kc as _moved

sys.modules[__name__] = _moved
