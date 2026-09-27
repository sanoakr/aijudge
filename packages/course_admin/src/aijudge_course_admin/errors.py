"""コース運営の操作が続けられないことを表す例外（段階 3-1）。

`apps/admin` の 20 近いモジュールが `operations` を import していたのは、ほとんど
この例外のためだけだった。例外が業務処理の塊（`operations`）の中にあると、
葉のモジュールを移すたびに `operations` ごと引きずることになる。
"""

from __future__ import annotations


class AdminError(Exception):
    """操作を続けられない。

    画面は 400 と文言に、CLI は終了コードと標準エラーに読み替える。文言は
    教員や運用者がそのまま読むので、何をすればよいかまで書く。
    """
