"""受講者名簿の読み込み。

学期の頭に 90 名分のアカウントを手で作るのは現実的でないので、既にある
名簿をそのまま読む。**Sharif Judge のユーザーリスト形式**に合わせてあるのは、
それが移行元に実在するファイルだから（`2025shj-user.txt` 等）。

    <login> <email> <password|RANDOM[n]> <role>
    t190054 t190054@mail.ryukoku.ac.jp RANDOM[8] student

login だけを並べた素朴な形式も読む（`2025stdlist.txt`）。

**パスワードは名簿に書かせない設計にしてある。** `RANDOM[n]` を既定とし、
生成した値は資格情報ファイルに 1 回だけ書き出す。名簿に平文で書いてあると、
それが Drive や git に残り続ける。
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

from aijudge_core import Role

# 既定のパスワード長。scrypt に掛けるので長さで殴る必要はないが、
# 学習者が手で打てる範囲で推測されない程度は要る。
DEFAULT_PASSWORD_LENGTH = 12

# 紛らわしい文字を除く。手で配る前提なので 0/O・1/l/I を混ぜない。
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")

_RANDOM = re.compile(r"^RANDOM\[(\d+)\]$", re.IGNORECASE)

# Sharif Judge の役割名 → コアの Role。
_ROLES: dict[str, Role] = {
    "student": Role.LEARNER,
    "learner": Role.LEARNER,
    "instructor": Role.INSTRUCTOR,
    "teacher": Role.INSTRUCTOR,
    "ta": Role.ASSISTANT,
    "assistant": Role.ASSISTANT,
    "admin": Role.ADMIN,
    "head_instructor": Role.ADMIN,
}


class RosterError(Exception):
    """名簿が読めない。行番号を添えて返す。"""


@dataclass(frozen=True)
class RosterEntry:
    login: str
    email: str | None
    role: Role
    # 明示されたパスワード。None なら生成する。
    password: str | None
    password_length: int = DEFAULT_PASSWORD_LENGTH

    @property
    def display_name(self) -> str:
        """氏名は名簿に無いので login を使う。

        本名を持ち込まないのは、無くても運用が成立するものを個人情報として
        抱えないため。必要になったら `--names` で別途与える形にする。
        """
        return self.login


def generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def parse_roster(text: str, *, default_role: Role = Role.LEARNER) -> tuple[RosterEntry, ...]:
    """名簿を読む。**壊れた行は黙って飛ばさず例外にする。**

    飛ばすと、受講者が 1 人足りないことに気づかないまま学期が始まる。
    その学生だけ提出できない。
    """
    entries: list[RosterEntry] = []
    seen: set[str] = set()

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()

        login = fields[0]
        if login in seen:
            raise RosterError(f"{number} 行目: login {login!r} が重複しています")
        seen.add(login)

        email: str | None = None
        password: str | None = None
        length = DEFAULT_PASSWORD_LENGTH
        role = default_role

        if len(fields) >= 2:
            email = fields[1] if "@" in fields[1] else None
        if len(fields) >= 3:
            match = _RANDOM.match(fields[2])
            if match is not None:
                length = int(match.group(1))
                if length < 8:
                    raise RosterError(
                        f"{number} 行目: パスワード長 {length} は短すぎます（8 以上）"
                    )
            else:
                password = fields[2]
        if len(fields) >= 4:
            name = fields[3].lower()
            if name not in _ROLES:
                raise RosterError(
                    f"{number} 行目: 役割 {fields[3]!r} が不明です（{sorted(_ROLES)} のいずれか）"
                )
            role = _ROLES[name]

        entries.append(
            RosterEntry(
                login=login,
                email=email,
                role=role,
                password=password,
                password_length=length,
            )
        )

    if not entries:
        raise RosterError("名簿に有効な行がありません")
    return tuple(entries)


def load_roster(path: Path, *, default_role: Role = Role.LEARNER) -> tuple[RosterEntry, ...]:
    if not path.is_file():
        raise RosterError(f"{path} がありません")
    return parse_roster(path.read_text(encoding="utf-8"), default_role=default_role)


def write_credentials(path: Path, rows: list[tuple[str, str]]) -> None:
    """生成したパスワードを書き出す。

    **標準出力には出さない。** 端末の履歴やログ、画面共有に残る。
    ファイルのパーミッションを 0600 にして、置き場所を利用者に明示する。

    このファイルは配布したら消す前提のもの。パスワードはハッシュしか
    保存していないので、失った場合は再発行になる（それが正しい）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 中身を書く前に権限を締める。書いてから chmod すると、その間に読める。
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    lines = ["# login\tpassword", "# 配布したら削除すること。再取得はできない（再発行になる）。"]
    lines.extend(f"{login}\t{password}" for login, password in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
