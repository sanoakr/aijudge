"""パスワードのハッシュ化。

**平文も可逆な変換も保存しない。** 使うのは scrypt（RFC 7914）で、
標準ライブラリに入っている。argon2 の方が新しいが、依存を 1 つ増やす価値が
あるのは「argon2 でなければ足りない」場合で、Phase 0 の規模ではない。
scrypt はメモリ困難な KDF として現役であり、パラメータを明示しておけば
後から上げられる。

保存形式にパラメータを埋め込むのが要点。埋め込まないと、パラメータを
強くした日に既存の利用者が全員ログインできなくなる。

    scrypt$n=16384,r=8,p=1$<salt-b64>$<hash-b64>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCHEME = "scrypt"
# 2026 年時点で対話ログインに許容できる範囲。1 回あたり十数ミリ秒。
# 上げるときはここだけを変える（既存のハッシュは自分のパラメータで検証される）。
DEFAULT_N = 16384
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

# 短すぎるパスワードは KDF では救えない。下限は運用の判断だが、
# 「無い」より「弱い既定」の方が危険なので型で持つ。
MIN_PASSWORD_LENGTH = 8


class WeakPassword(ValueError):
    """パスワードが要件を満たさない。"""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"パスワードは {MIN_PASSWORD_LENGTH} 文字以上にしてください")
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES)
    return f"{SCHEME}$n={n},r={r},p={p}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """検証する。形式が壊れていても例外を投げず False を返す。

    例外にすると、壊れたハッシュを持つ利用者でログイン画面が 500 になり、
    「その利用者だけ壊れている」ことが攻撃者から観測できる。
    """
    try:
        scheme, parameters, salt_text, hash_text = encoded.split("$")
        if scheme != SCHEME:
            return False
        values = dict(item.split("=") for item in parameters.split(","))
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_text),
            n=int(values["n"]),
            r=int(values["r"]),
            p=int(values["p"]),
            dklen=KEY_BYTES,
        )
    except (ValueError, KeyError, TypeError):
        return False
    # 一致・不一致で処理時間が変わらない比較。
    return hmac.compare_digest(derived, _unb64(hash_text))


def needs_rehash(encoded: str, *, n: int = DEFAULT_N) -> bool:
    """保存されたハッシュが現在のパラメータより弱いか。

    ログイン成功時に作り直すのに使う。パラメータを上げても、既存の利用者は
    次のログインで自動的に強い方へ移る。
    """
    try:
        _, parameters, _, _ = encoded.split("$")
        values = dict(item.split("=") for item in parameters.split(","))
        return int(values["n"]) < n
    except (ValueError, KeyError):
        return True
