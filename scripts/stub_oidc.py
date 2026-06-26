#!/usr/bin/env python3
"""
stub_oidc.py — a tiny stand-in for Google's OIDC token + userinfo endpoints.

Lets us verify the relay's OAuth gate (relay/src/google_gate.ts) end to end with no
browser and no real Google: the Worker is pointed at this server via the
GOOGLE_TOKEN_URL / GOOGLE_USERINFO_URL env overrides (set in relay/.dev.vars).

The identity returned is encoded in the fake authorization `code`, so one stub covers
the allow / deny / unverified cases:

    code "code-allowed"     → (alice@example.com, email_verified=True)   ← allowlisted
    code "code-denied"      → (mallory@example.com, email_verified=True) ← not allowed
    code "code-unverified"  → (alice@example.com, email_verified=False)  ← unverified

Flow mirrored: /token echoes the code back as the access_token; /userinfo maps that
token (= the code) to the (email, email_verified) pair.

Run via scripts/test_oauth.py, or manually:
    uvicorn scripts.stub_oidc:app --port 8799
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# code (== access_token) → (email, email_verified)
_ACCOUNTS = {
    "code-allowed": ("alice@example.com", True),
    "code-denied": ("mallory@example.com", True),
    "code-unverified": ("alice@example.com", False),
}


async def token(req: Request) -> JSONResponse:
    form = await req.form()
    code = str(form.get("code", ""))
    # Echo the code as the access token so /userinfo can map it back.
    return JSONResponse({"access_token": code, "token_type": "Bearer", "expires_in": 3600})


async def userinfo(req: Request) -> JSONResponse:
    auth = req.headers.get("authorization", "")
    token_val = auth[len("Bearer "):] if auth.lower().startswith("bearer ") else ""
    email, verified = _ACCOUNTS.get(token_val, ("", False))
    return JSONResponse({"email": email, "email_verified": verified, "sub": token_val})


app = Starlette(routes=[
    Route("/token", token, methods=["POST"]),
    Route("/userinfo", userinfo, methods=["GET"]),
])
