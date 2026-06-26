#!/usr/bin/env python3
"""
test_oauth.py — Step-2 verification of the relay's OAuth authorization server.

Proves the claude.ai-facing auth, with NO browser and NO real Google, by pointing the
Worker's Google endpoints at scripts/stub_oidc.py (via relay/.dev.vars). It drives the
full OAuth dance over HTTP against a running `wrangler dev`:

    1. both /.well-known metadata docs serve
    2. /mcp with no token → 401 WITH the RFC 9728 WWW-Authenticate: resource_metadata
       header (the exact header claude.ai requires — CLAUDE.md "Auth")
    3. DCR /register → /authorize → /oauth/google/callback (allowlisted, verified) →
       /token → opaque access token → /mcp Bearer reaches the echo tool
    4. a non-allowlisted email is denied at the callback (no token issued)
    5. an unverified email is denied at the callback

Prereq: a Worker dev server (this drives, it does not start it):

    cd relay && npx wrangler dev --port 8787          # terminal 1
    .venv/bin/python scripts/test_oauth.py            # terminal 2 (spawns stub+agent)

It spawns scripts/stub_oidc.py (:8799) and relay/relay_client.py (echo agent,
identity alice@example.com) itself; pass --no-spawn to manage those yourself.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

RELAY_HTTP = os.environ.get("RELAY_HTTP", "http://127.0.0.1:8787")
RELAY_WS = os.environ.get("RELAY_WS", "ws://127.0.0.1:8787/agent")
STUB_PORT = 8799
ALLOWED_EMAIL = "alice@example.com"          # must match relay/.dev.vars allowlist
CLIENT_REDIRECT = "http://127.0.0.1:9999/cb"  # never actually fetched


async def _wait_port(host: str, port: int, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, w = await asyncio.open_connection(host, port)
            w.close()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise TimeoutError(f"{host}:{port} not up after {timeout}s")


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def check_metadata(c: httpx.AsyncClient) -> None:
    pr = await c.get(f"{RELAY_HTTP}/.well-known/oauth-protected-resource")
    assert pr.status_code == 200, f"protected-resource metadata: {pr.status_code}"
    assert pr.json().get("authorization_servers"), "no authorization_servers in PR metadata"
    asd = await c.get(f"{RELAY_HTTP}/.well-known/oauth-authorization-server")
    assert asd.status_code == 200, f"authorization-server metadata: {asd.status_code}"
    body = asd.json()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert body.get(key), f"AS metadata missing {key}"
    print("  ✓ both /.well-known metadata docs serve")


async def check_unauthenticated(c: httpx.AsyncClient) -> None:
    r = await c.post(f"{RELAY_HTTP}/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert r.status_code == 401, f"expected 401 for tokenless /mcp, got {r.status_code}"
    www = r.headers.get("www-authenticate", "")
    assert "resource_metadata" in www, f"401 missing RFC 9728 resource_metadata header: {www!r}"
    print(f"  ✓ tokenless /mcp → 401 with WWW-Authenticate: {www}")


async def register_client(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{RELAY_HTTP}/register", json={
        "client_name": "step2-test",
        "redirect_uris": [CLIENT_REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    assert r.status_code in (200, 201), f"DCR /register failed: {r.status_code} {r.text}"
    cid = r.json()["client_id"]
    print(f"  ✓ DCR registered client_id={cid}")
    return cid


async def run_flow(c: httpx.AsyncClient, client_id: str, google_code: str) -> dict:
    """Drive /authorize → google callback → /token. Returns {'token':..} or {'denied':status}."""
    verifier, challenge = _pkce()
    client_state = secrets.token_urlsafe(8)

    # /authorize → 302 to the (stubbed) Google auth URL carrying our relay `state`.
    authz = await c.get(f"{RELAY_HTTP}/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": client_state,
        "scope": "openid email",
    })
    assert authz.status_code == 302, f"/authorize → {authz.status_code} (want 302)"
    relay_state = parse_qs(urlparse(authz.headers["location"]).query).get("state", [""])[0]
    assert relay_state, "no state forwarded to Google"

    # Google redirects back to our callback with the fake code.
    cb = await c.get(f"{RELAY_HTTP}/oauth/google/callback",
                     params={"code": google_code, "state": relay_state})
    if cb.status_code != 302:
        return {"denied": cb.status_code, "body": cb.text}

    q = parse_qs(urlparse(cb.headers["location"]).query)
    assert q.get("state", [""])[0] == client_state, "client state not echoed back"
    auth_code = q.get("code", [""])[0]
    assert auth_code, "no MCP authorization code from callback"

    tok = await c.post(f"{RELAY_HTTP}/token", data={
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": CLIENT_REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    assert tok.status_code == 200, f"/token → {tok.status_code} {tok.text}"
    body = tok.json()
    assert body.get("token_type", "").lower() == "bearer"
    return {"token": body["access_token"]}


async def echo_with_token(token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(f"{RELAY_HTTP}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            assert "echo" in names, f"echo not advertised: {names}"
            res = await s.call_tool("echo", {"text": "oauth works"})
            text = res.content[0].text
            assert text == "echo: oauth works", f"unexpected echo: {text!r}"
    print("  ✓ opaque access token reaches the echo tool through the relay")


async def run_checks() -> int:
    async with httpx.AsyncClient(follow_redirects=False, timeout=20) as c:
        await check_metadata(c)
        await check_unauthenticated(c)
        client_id = await register_client(c)

        allowed = await run_flow(c, client_id, "code-allowed")
        assert "token" in allowed, f"allowlisted sign-in did not yield a token: {allowed}"
        await echo_with_token(allowed["token"])

        denied = await run_flow(c, client_id, "code-denied")
        assert denied.get("denied") == 403, f"non-allowlisted email not denied: {denied}"
        assert "not authorized" in denied.get("body", ""), "deny page missing expected text"
        print("  ✓ non-allowlisted email denied at callback (no token)")

        unver = await run_flow(c, client_id, "code-unverified")
        assert unver.get("denied") == 403, f"unverified email not denied: {unver}"
        print("  ✓ unverified email denied at callback (no token)")

    print("\n✅ PASS — Step 2 OAuth gate: discovery, 401+WWW-Authenticate, "
          "Google allowlist, opaque token → echo")
    return 0


async def main() -> int:
    spawn = "--no-spawn" not in sys.argv
    procs = []
    if spawn:
        py = sys.executable
        root = os.path.join(os.path.dirname(__file__), "..")
        print(f"· starting stub OIDC on :{STUB_PORT}")
        procs.append(await asyncio.create_subprocess_exec(
            py, "-m", "uvicorn", "scripts.stub_oidc:app", "--port", str(STUB_PORT),
            "--log-level", "warning", cwd=root))
        await _wait_port("127.0.0.1", STUB_PORT)

        print("· starting relay/relay_client.py (echo agent, identity alice@example.com)")
        env = {**os.environ, "RELAY_URL": RELAY_WS, "RELAY_IDENTITY": ALLOWED_EMAIL}
        relay_client = os.path.join(root, "relay", "relay_client.py")
        procs.append(await asyncio.create_subprocess_exec(py, relay_client, env=env))
        await asyncio.sleep(2.0)  # let the agent register its WS

    try:
        return await run_checks()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
