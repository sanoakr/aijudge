"""Google OAuth2（OIDC）ログインアダプタ（#121・#124）。

**認証方式の差し替えであって、認可（コース・役割）の模型は変えない。**
`AuthService` の既存のセッション発行を通すので、ここで解決した利用者は
`AuthService.resolve` でローカル利用者と区別なく引ける。

このリポジトリは公開物である。大学固有の値（ドメイン・client_id/secret・
redirect URI）はここにも他のどこにも書かない ── `OidcSettings` はテナント
ごとに管理者が設定する値（`packages/persistence` の DB に保存する）。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import TenantId

from .errors import AuthenticationFailed

# Google の固定エンドポイント。テナントごとに変わるのは client_id/secret と
# 許可ドメインだけ（他の IdP を足すなら、この定数を持つ別クラスを追加する
# ── ADR 0004 の llm_gateway と同じ「プロバイダを足すのはアダプタの追加」）。
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"

#: ログインボタンの既定の文言（#209）。**機関名を入れない。**
DEFAULT_LOGIN_LABEL = "大学アカウントでログイン"
#: 同じ文言の上限。**画面と模型が同じ数を見るために名前で持つ**（#212）──
#: 画面の `maxlength` だけに書くと、それを無視した要求が模型の検証まで届き、
#: 用意してある案内ではなく 500 になる。
LOGIN_LABEL_MAX = 64


class OidcSettings(BaseModel):
    """テナント単位の OIDC 設定。

    **`client_secret` はこの型の中では常に平文。** DB に保存する瞬間に
    暗号化するのは `packages/persistence` の責務（`aijudge_identity` は
    import-linter 上 sqlalchemy/persistence に触れない）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    # 1 機関が複数ドメインを許すこともあるので単一値にしない。
    allowed_domains: tuple[str, ...] = Field(min_length=1)
    issuer: str = "https://accounts.google.com"
    #: ログイン画面のボタンに出す文言（#209）。
    #:
    #: **機関ごとに呼び名が違う**（「全学認証アカウント」「統合認証」…）。
    #: `AIJUDGE_DEMO_INSTRUCTOR_PREFIX` と同じ理由で、機関の語彙をコードに
    #: 焼き込まない ── **既定は機関に依らない一般的な文言**にしてあり、
    #: リポジトリが公開物である以上ここに機関名は入らない。
    login_label: str = Field(default=DEFAULT_LOGIN_LABEL, min_length=1, max_length=LOGIN_LABEL_MAX)


@dataclass(frozen=True)
class GoogleOidcIdentity:
    """ドメイン制限まで済ませた、突合後の Google 利用者。"""

    sub: str
    email: str
    hd: str | None


class GoogleOidcProvider:
    """Google の認可コードフロー。JWT 署名検証は joserfc に任せ、自前で書かない。"""

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        # テストは `httpx.MockTransport` を積んだクライアントを注入する。
        self._http = http_client if http_client is not None else httpx.Client(timeout=10.0)
        self._owns_http = http_client is None

    def authorization_url(
        self, settings: OidcSettings, *, redirect_uri: str
    ) -> tuple[str, str, str]:
        """認可エンドポイントへの URL と、あとで突き合わせる state・nonce を返す。

        state・nonce はここで発行するだけ ── セッションへの一時保存は
        呼び出し側（`apps/*` のログインルート、#125）の仕事。

        **`prompt=select_account` を必ず付ける**（#208）。付けないと Google は
        ブラウザに残っているセッションを黙って再利用するので、私物の口座で
        サインイン済みの端末からは大学の口座を選べない ── しかも弾くのは
        Google なので、利用者には aiJudge の不具合として見える。選ばせる方が
        1 手多いが、**選べない状態からの復帰には別タブでの口座切り替えが要る**。
        """
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        params = {
            "client_id": settings.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "prompt": "select_account",
        }
        # **`hd` は候補を絞る助けであって、検査ではない**（#208）。URL の
        # 書き換えで外せるものに機関の境界を預けない ── 突合後のドメイン検査
        # （`exchange_code`）が境界で、こちらはそのままにする。
        #
        # 許可ドメインが 2 つ以上ある機関では付けない。`hd` は 1 つしか取れず、
        # 片方を選ぶと**もう片方の在学者が選択画面で自分の口座を見失う**。
        if len(settings.allowed_domains) == 1:
            params["hd"] = settings.allowed_domains[0]
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}", state, nonce

    def exchange_code(
        self,
        settings: OidcSettings,
        *,
        code: str,
        redirect_uri: str,
        expected_state: str,
        actual_state: str,
        expected_nonce: str,
    ) -> GoogleOidcIdentity:
        """`code` を交換し、検証済みの ID トークンから利用者を取り出す。

        state 不一致・nonce 不一致・署名検証失敗・許可ドメイン外は、
        理由を分けず全て `AuthenticationFailed` にする ── ローカル認証の
        `AuthenticationFailed`（「存在しない ID」と「違うパスワード」を
        分けない）と同じ理屈で、失敗の内訳を外に漏らさない。
        """
        if not secrets.compare_digest(expected_state, actual_state):
            raise AuthenticationFailed("ログインの状態を確認できませんでした")

        token_response = self._http.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_response.status_code != 200:
            raise AuthenticationFailed("Google との認証に失敗しました")
        id_token = token_response.json().get("id_token")
        if not id_token:
            raise AuthenticationFailed("Google との認証に失敗しました")

        jwks_response = self._http.get(JWKS_ENDPOINT)
        jwks_response.raise_for_status()
        key_set = KeySet.import_key_set(jwks_response.json())
        try:
            token = jwt.decode(id_token, key_set)
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "values": [settings.issuer, "accounts.google.com"]},
                aud={"essential": True, "value": settings.client_id},
                exp={"essential": True},
            ).validate(token.claims)
        except JoseError as exc:
            raise AuthenticationFailed("Google のトークンを検証できませんでした") from exc
        claims = token.claims

        # iss・aud・exp は上の JWTClaimsRegistry が検証済み。
        if claims.get("nonce") != expected_nonce:
            raise AuthenticationFailed("ログインの状態を確認できませんでした")

        sub = claims.get("sub")
        email = claims.get("email")
        if not sub or not email:
            raise AuthenticationFailed("Google のトークンに必要な情報がありません")

        # **確認済みのメールでなければ通さない**（#220）。下のドメイン検査は
        # `hd` が無いときメールの後ろを見るので、確認していないアドレスを
        # 受け入れると「その機関のドメインを名乗るだけ」で境界を越えられる。
        # #208 で `hd` を足したときの「境界は突合後の検査の側にある」という
        # 判断は、この検査が効いていることを前提にしている。
        if claims.get("email_verified") is not True:
            raise AuthenticationFailed("このアカウントではログインできません")

        hd = claims.get("hd")
        domain = hd or email.rsplit("@", 1)[-1]
        if domain not in settings.allowed_domains:
            raise AuthenticationFailed("このドメインのアカウントではログインできません")

        return GoogleOidcIdentity(sub=sub, email=email, hd=hd)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
