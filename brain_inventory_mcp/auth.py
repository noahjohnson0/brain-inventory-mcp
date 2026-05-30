"""Self-contained, single-user OAuth 2.1 for the inventory MCP server.

The MCP SDK mounts the standard OAuth endpoints (/authorize, /token, /register,
/revoke, and the metadata documents) and verifies PKCE for us. We supply the
provider behind them plus a /login password gate, so the whole authorization
server lives in this process with no third party.

Model: there is exactly one resource owner (the vault owner). Any OAuth client (the
Claude app, via Dynamic Client Registration) can register, but issuing a code requires
entering the shared password set in BRAIN_MCP_PASSWORD. Tokens are opaque random
strings persisted to a 0600 JSON file so they survive restarts.

Security-relevant choices:
- Refuse to run the public HTTP transport without BRAIN_MCP_PASSWORD set.
- Password compared with hmac.compare_digest (constant time).
- Authorization codes are single-use and expire in 5 minutes.
- Refresh tokens rotate on every use; the old one is revoked.
- The store file is created with 0600 perms.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "inventory"
ACCESS_TTL = 3600              # 1 hour
REFRESH_TTL = 60 * 60 * 24 * 30  # 30 days
CODE_TTL = 300                 # 5 minutes
SUBJECT = "owner"


def store_path() -> Path:
    raw = os.environ.get(
        "BRAIN_MCP_STATE", "~/.config/brain-inventory-mcp/oauth.json"
    )
    return Path(os.path.expanduser(raw))


class _Store:
    """Tiny JSON-backed, lock-guarded persistence for OAuth state."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {
            "clients": {},
            "pending": {},
            "codes": {},
            "access": {},
            "refresh": {},
        }
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)


def _password() -> str:
    pw = os.environ.get("BRAIN_MCP_PASSWORD", "")
    if not pw:
        raise RuntimeError(
            "BRAIN_MCP_PASSWORD is required to run the authenticated HTTP server."
        )
    return pw


def public_url() -> str:
    url = os.environ.get("BRAIN_MCP_PUBLIC_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "BRAIN_MCP_PUBLIC_URL (e.g. https://xyz.trycloudflare.com) is required "
            "so OAuth metadata advertises the correct public address."
        )
    return url


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self) -> None:
        self.store = _Store(store_path())

    # --- Dynamic Client Registration -------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self.store.lock:
            self.store.data["clients"][client_info.client_id] = client_info.model_dump(
                mode="json", exclude_none=True
            )
            self.store.save()

    # --- Authorization (browser leg) -------------------------------------
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Stash the request and bounce the browser to our password page."""
        login_id = secrets.token_urlsafe(24)
        with self.store.lock:
            self.store.data["pending"][login_id] = {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "resource": params.resource,
                "expires_at": time.time() + CODE_TTL,
            }
            self.store.save()
        return f"{public_url()}/login?{urlencode({'login_id': login_id})}"

    def complete_login(self, login_id: str, password: str) -> str | None:
        """Validate password, mint an auth code, return the client redirect URL.

        Returns None if the login_id is unknown/expired. Raises PermissionError on
        a bad password.
        """
        with self.store.lock:
            pending = self.store.data["pending"].get(login_id)
            if not pending or pending["expires_at"] < time.time():
                self.store.data["pending"].pop(login_id, None)
                self.store.save()
                return None
            if not hmac.compare_digest(password, _password()):
                raise PermissionError("bad password")
            code = secrets.token_urlsafe(32)
            self.store.data["codes"][code] = {
                "code": code,
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending[
                    "redirect_uri_provided_explicitly"
                ],
                "code_challenge": pending["code_challenge"],
                "scopes": pending["scopes"],
                "resource": pending["resource"],
                "expires_at": time.time() + CODE_TTL,
                "subject": SUBJECT,
            }
            self.store.data["pending"].pop(login_id, None)
            self.store.save()
            return construct_redirect_uri(
                pending["redirect_uri"], code=code, state=pending["state"]
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        raw = self.store.data["codes"].get(authorization_code)
        if not raw or raw["client_id"] != client.client_id:
            return None
        if raw["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            code=raw["code"],
            scopes=raw["scopes"],
            expires_at=raw["expires_at"],
            client_id=raw["client_id"],
            code_challenge=raw["code_challenge"],
            redirect_uri=AnyUrl(raw["redirect_uri"]),
            redirect_uri_provided_explicitly=raw["redirect_uri_provided_explicitly"],
            resource=raw["resource"],
            subject=raw["subject"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        with self.store.lock:
            if authorization_code.code not in self.store.data["codes"]:
                raise TokenError("invalid_grant", "authorization code already used")
            self.store.data["codes"].pop(authorization_code.code, None)
            token = self._issue(
                authorization_code.client_id,
                authorization_code.scopes,
                authorization_code.resource,
            )
            self.store.save()
        return token

    # --- Refresh ---------------------------------------------------------
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = self.store.data["refresh"].get(refresh_token)
        if not raw or raw["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate(raw)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self.store.lock:
            # rotate: kill the presented refresh token
            self.store.data["refresh"].pop(refresh_token.token, None)
            granted = scopes or refresh_token.scopes
            token = self._issue(refresh_token.client_id, granted, None)
            self.store.save()
        return token

    # --- Access token verification (called per MCP request) --------------
    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self.store.data["access"].get(token)
        if not raw:
            return None
        if raw["expires_at"] and raw["expires_at"] < time.time():
            with self.store.lock:
                self.store.data["access"].pop(token, None)
                self.store.save()
            return None
        return AccessToken.model_validate(raw)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.store.lock:
            self.store.data["access"].pop(token.token, None)
            self.store.data["refresh"].pop(token.token, None)
            self.store.save()

    # --- helpers ---------------------------------------------------------
    def _issue(
        self, client_id: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        """Mint a fresh access+refresh pair. Caller holds the lock and saves."""
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        self.store.data["access"][access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TTL,
            resource=resource,
            subject=SUBJECT,
        ).model_dump(mode="json")
        self.store.data["refresh"][refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TTL,
            subject=SUBJECT,
        ).model_dump(mode="json")
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


def transport_security() -> TransportSecuritySettings:
    """Allow the public tunnel Host/Origin through DNS-rebinding protection.

    We bind to 127.0.0.1, so the SDK would otherwise only trust localhost and
    reject the tunnel's Host header with 421 Misdirected Request.
    """
    from urllib.parse import urlparse

    host = urlparse(public_url()).netloc  # e.g. xyz.trycloudflare.com
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"https://{host}", "http://127.0.0.1:*", "http://localhost:*"],
    )


def auth_settings() -> AuthSettings:
    base = public_url()
    return AuthSettings(
        issuer_url=AnyUrl(base),
        resource_server_url=AnyUrl(f"{base}/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[SCOPE],
            default_scopes=[SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
    )


_LOGIN_PAGE = """<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>brain inventory — sign in</title>
<style>
 body{{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;
   display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
 form{{background:#1c1c1e;padding:2rem;border-radius:14px;width:min(92vw,340px);
   box-shadow:0 8px 40px rgba(0,0,0,.5)}}
 h1{{font-size:1.1rem;margin:0 0 1rem}} .muted{{color:#888;font-size:.8rem;margin-top:.75rem}}
 input{{width:100%;box-sizing:border-box;padding:.7rem;border-radius:9px;border:1px solid #333;
   background:#000;color:#fff;font-size:1rem}}
 button{{width:100%;margin-top:.9rem;padding:.7rem;border:0;border-radius:9px;
   background:#0a84ff;color:#fff;font-size:1rem;font-weight:600}}
 .err{{color:#ff6b6b;font-size:.85rem;margin-top:.6rem}}
</style></head><body>
<form method="post" action="/login">
 <h1>🧠 brain inventory</h1>
 <input type="hidden" name="login_id" value="{login_id}">
 <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
 <button type="submit">Authorize Claude</button>
 {error}
 <div class="muted">Connecting the Claude app to your private inventory.</div>
</form></body></html>"""


def register_login_routes(mcp, provider: SingleUserOAuthProvider) -> None:
    """Attach the GET/POST /login password gate to the FastMCP app."""

    @mcp.custom_route("/login", methods=["GET"])
    async def login_form(request: Request) -> Response:  # type: ignore[unused-ignore]
        login_id = request.query_params.get("login_id", "")
        return HTMLResponse(_LOGIN_PAGE.format(login_id=login_id, error=""))

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request) -> Response:  # type: ignore[unused-ignore]
        form = await request.form()
        login_id = str(form.get("login_id", ""))
        password = str(form.get("password", ""))
        try:
            target = provider.complete_login(login_id, password)
        except PermissionError:
            return HTMLResponse(
                _LOGIN_PAGE.format(
                    login_id=login_id,
                    error='<div class="err">Wrong password.</div>',
                ),
                status_code=401,
            )
        if target is None:
            return HTMLResponse(
                _LOGIN_PAGE.format(
                    login_id="",
                    error='<div class="err">Login expired. Restart from the Claude app.</div>',
                ),
                status_code=400,
            )
        return RedirectResponse(target, status_code=302)
