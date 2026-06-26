"""
Single-user OAuth 2.1 gate for the Marketplace Finder MCP server, gated by
Google sign-in (restricted to an email allowlist).

WHY THIS EXISTS
---------------
claude.ai will only authenticate to a remote MCP server that speaks the MCP
authorization spec — OAuth 2.1 (authorization-code + PKCE) with Dynamic Client
Registration. A static bearer token does NOT work: claude.ai's custom-connector
UI has no field to send a custom header (this is why `MCP_AUTH_TOKEN` can't lock
down the claude.ai path — see CLAUDE.md). And putting Cloudflare Access in front
is broken for claude.ai web/mobile (anthropics/claude-ai-mcp#410: Access omits
the RFC 9728 `WWW-Authenticate` header that claude.ai requires).

So we run a *minimal authorization server inside this MCP server*, and broker a
second login to Google. The MCP SDK implements the hard OAuth parts (the
/authorize, /token, /register, /revoke endpoints, PKCE verification,
redirect_uri validation, the metadata documents, and crucially the
`WWW-Authenticate: Bearer resource_metadata="..."` header that claude.ai needs).
We implement the actual human gate:

    THE GATE is Google sign-in. /authorize redirects the user's browser to
    Google; on return we verify the account's email against MCP_ALLOWED_EMAILS
    and only then issue a token. Without a valid token every /mcp request is 401.

This is the broker pattern the SDK's OAuthAuthorizationServerProvider.authorize
docstring describes:  claude.ai ──▶ this MCP server ──▶ Google.

State (registered clients + issued tokens) is persisted to a gitignored JSON
file so the launchd service can restart (it KeepAlive-restarts) without forcing
you to reconnect claude.ai every time.

This is a single-user / small-allowlist design: it is not built for multi-tenant
use.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger("marketplace.oauth")

AUTH_CODE_TTL = 300                      # authorization code: 5 minutes
ACCESS_TOKEN_TTL = 60 * 60 * 24          # access token: 1 day (claude.ai refreshes)
# Refresh tokens do not expire; they are rotated on every use.

# Google OIDC endpoints (overridable as instance attrs for testing).
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_CALLBACK_PATH = "/oauth/google/callback"


class _Store:
    """Tiny JSON-file-backed store for DCR clients and issued tokens.

    Persisted so a server restart doesn't invalidate claude.ai's connection.
    Authorization codes and in-flight authorize requests are deliberately NOT
    persisted — they are short-lived and safe to drop on restart.
    """

    def __init__(self, path: Path):
        self.path = path
        self.clients: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.clients = data.get("clients", {})
            self.access = data.get("access", {})
            self.refresh = data.get("refresh", {})
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupt store — start fresh rather than crash
            log.warning("oauth store unreadable (%s); starting empty", e)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"clients": self.clients, "access": self.access, "refresh": self.refresh}
        ))
        tmp.replace(self.path)          # atomic
        try:
            self.path.chmod(0o600)      # tokens are secrets — owner-only
        except OSError:
            pass


class GoogleOAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth provider whose authorization check is Google sign-in + an email allowlist."""

    def __init__(
        self,
        *,
        google_client_id: str,
        google_client_secret: str,
        allowed_emails: list[str],
        public_url: str,
        store_path: str,
    ):
        if not (google_client_id and google_client_secret):
            raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required")
        if not allowed_emails:
            raise ValueError("MCP_ALLOWED_EMAILS must list at least one address")
        self.google_client_id = google_client_id
        self.google_client_secret = google_client_secret
        self.allowed_emails = {e.strip().lower() for e in allowed_emails if e.strip()}
        self.public_url = public_url.rstrip("/")
        self.redirect_uri = self.public_url + GOOGLE_CALLBACK_PATH
        self.store = _Store(Path(store_path))
        self._pending: dict[str, tuple[str, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # ── Dynamic Client Registration ────────────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.clients[client_info.client_id] = client_info.model_dump(mode="json")
        self.store.save()
        log.info("registered OAuth client %s (%s)", client_info.client_id, client_info.client_name)

    # ── /authorize → bounce the browser to Google sign-in ──────────────────
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = secrets.token_urlsafe(32)            # our key into _pending
        self._pending[state] = (client.client_id, params)
        google_url = GOOGLE_AUTH_URL + "?" + urlencode({
            "client_id": self.google_client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        })
        return google_url

    # ── Google redirect lands here (custom route in src/server.py) ─────────
    async def google_callback(self, *, code: str, state: str, error: str | None) -> Response:
        entry = self._pending.pop(state, None)
        if entry is None:
            return HTMLResponse(_PAGE_EXPIRED, status_code=400)
        client_id, params = entry

        if error or not code:
            return self._deny(f"Google sign-in was cancelled ({html.escape(error or 'no code')}).")

        try:
            email, verified = await self._fetch_email(code)
        except Exception as e:                       # network / Google error
            log.warning("google token exchange failed: %s", e)
            return self._deny("Could not verify your Google account. Try again.")

        if not verified or email.lower() not in self.allowed_emails:
            log.warning("denied sign-in for %r (verified=%s)", email, verified)
            return self._deny(f"{html.escape(email or 'This account')} is not authorized for this server.")

        # Approved — mint an MCP authorization code and return to claude.ai.
        auth_code = secrets.token_urlsafe(32)
        self._codes[auth_code] = AuthorizationCode(
            code=auth_code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,    # SDK verifies PKCE at /token
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=email.lower(),
        )
        log.info("approved sign-in for %s (client %s)", email, client_id)
        target = construct_redirect_uri(str(params.redirect_uri), code=auth_code, state=params.state)
        return RedirectResponse(url=target, status_code=302)

    async def _fetch_email(self, code: str) -> tuple[str, bool]:
        """Exchange the Google auth code and return (email, email_verified).

        We trust the userinfo response because the access token came straight
        from Google's token endpoint over TLS using our own client secret —
        no separate JWT-signature check is needed for that path.
        """
        import httpx  # lazy: only needed when a Google exchange actually runs

        async with httpx.AsyncClient(timeout=15) as c:
            tok = await c.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": self.google_client_id,
                "client_secret": self.google_client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            })
            tok.raise_for_status()
            access_token = tok.json()["access_token"]
            info = await c.get(GOOGLE_USERINFO_URL,
                               headers={"Authorization": f"Bearer {access_token}"})
            info.raise_for_status()
            data = info.json()
        return data.get("email", ""), bool(data.get("email_verified"))

    # ── token endpoints (PKCE + redirect_uri checks done by the SDK) ───────
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        ac = self._codes.get(authorization_code)
        if ac and ac.client_id == client.client_id and ac.expires_at >= time.time():
            return ac
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self.store.refresh.get(refresh_token)
        if not data:
            return None
        rt = RefreshToken.model_validate(data)
        return rt if rt.client_id == client.client_id else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self.store.refresh.pop(refresh_token.token, None)     # rotate
        self.store.save()
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.access.get(token)
        if not data:
            return None
        at = AccessToken.model_validate(data)
        if at.expires_at and at.expires_at < time.time():
            self.store.access.pop(token, None)
            self.store.save()
            return None
        return at

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        tok = getattr(token, "token", None)
        if tok:
            self.store.access.pop(tok, None)
            self.store.refresh.pop(tok, None)
            self.store.save()

    # ── helpers ────────────────────────────────────────────────────────────
    def _issue(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.store.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL, subject=subject,
        ).model_dump(mode="json")
        self.store.refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes, subject=subject,
        ).model_dump(mode="json")
        self.store.save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    def _deny(self, message: str) -> HTMLResponse:
        return HTMLResponse(_PAGE_DENIED.format(message=message), status_code=403)


# ── minimal styled result pages ────────────────────────────────────────────
_STYLE = (
    "font-family:-apple-system,system-ui,sans-serif;max-width:24rem;margin:18vh auto;"
    "padding:2rem;border:1px solid #e5e5e5;border-radius:12px;text-align:center"
)

_PAGE_DENIED = f"""<!doctype html><meta charset=utf-8><title>Access denied</title>
<div style="{_STYLE}"><h2>Access denied</h2><p>{{message}}</p>
<p style="color:#888;font-size:.85rem">Close this window and try again from claude.ai.</p></div>"""

_PAGE_EXPIRED = f"""<!doctype html><meta charset=utf-8><title>Request expired</title>
<div style="{_STYLE}"><h2>Request expired</h2>
<p>This authorization request is no longer valid. Start over from claude.ai.</p></div>"""
