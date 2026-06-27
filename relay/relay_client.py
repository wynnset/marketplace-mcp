#!/usr/bin/env python3
"""
relay_client.py — Mac-side reverse tunnel  (STEP 1 SKELETON)

Proves the new transport end to end:

    claude.ai ──HTTPS──▶ Cloudflare Worker ──▶ Durable Object ──WSS──▶ THIS ──▶ back

Instead of accepting inbound HTTP (the old cloudflared model), this process dials
*outbound* to the relay over a WebSocket and registers. The relay forwards each
claude.ai HTTP request to /mcp down that socket as a small envelope; we replay it
into a real in-process FastMCP app and ship the HTTP response back up the socket.

Why this shape: it reuses the exact FastMCP app (stateless_http + json_response)
with zero MCP-protocol re-implementation — the relay is a pure request/response
HTTP-over-WebSocket tunnel, which is only possible *because* every tool call is a
single JSON response (no SSE stream to keep alive). The whole class of cloudflared
QUIC/SSE drops (CLAUDE.md lesson #3) simply cannot happen here.

The app exposes a single `echo` tool; step 4 swaps it for the real Facebook tools
(same app object, same transport). Identity on the real Worker comes from a signed
**device-JWT** (`RELAY_DEVICE_TOKEN`): we present it as `Authorization: Bearer …` on
the WebSocket upgrade, and the Worker derives the routing identity from its verified
`sub`. The legacy `X-Relay-Identity` header is still sent for the offline mock relay
(scripts/mock_relay.py), which the real Worker ignores.

Env:
  RELAY_URL           wss://mcp.<domain>/agent   (default ws://127.0.0.1:8787/agent)
  RELAY_DEVICE_TOKEN  signed device-JWT minted by `finder` (real Worker; default none)
  RELAY_IDENTITY      legacy routing key for the mock relay only (default "default")
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys

import httpx
import websockets

log = logging.getLogger("relay")

RELAY_URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8787/agent")
RELAY_DEVICE_TOKEN = os.environ.get("RELAY_DEVICE_TOKEN", "")    # signed device-JWT
RELAY_IDENTITY = os.environ.get("RELAY_IDENTITY", "default")    # legacy mock routing
RECONNECT_MIN, RECONNECT_MAX = 1, 30
MAX_FRAME = 16 * 1024 * 1024

# Request headers we must NOT forward verbatim into the in-process app: host (would
# trip rebinding checks / set a bogus authority) and length/connection framing that
# httpx recomputes from the body we hand it.
_SKIP_REQ_HEADERS = {"host", "content-length", "connection", "x-relay-identity"}


# ── the MCP app: the REAL Facebook tools from src/server.py ────────────────────
# Serve the same FastMCP app the standalone server exposes — search_facebook_
# marketplace + get_listing_details, with the Chromium pre-warm lifespan — over
# the reverse tunnel, with zero MCP-protocol re-implementation. It still runs
# stateless_http + json_response (so the relay stays a pure request/response
# proxy) and with DNS-rebinding protection OFF: there is no public host to pin
# here — the Worker terminates claude.ai's TLS/Host and owns that check.
#
# On the Mac this app runs OPEN: the relay Worker is the authenticated boundary
# (Google OAuth + per-identity routing), so we clear any local auth/host gate
# before importing server.py to guarantee no second gate activates here.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
for _gate in ("GOOGLE_CLIENT_ID", "MCP_ALLOWED_HOSTS", "MCP_AUTH_TOKEN"):
    os.environ.pop(_gate, None)

import server  # noqa: E402  (src/server.py — added to sys.path just above)

app = server.app


# ── dispatch one forwarded HTTP request into the app, return a response envelope ─
async def _dispatch(client: httpx.AsyncClient, env: dict) -> dict:
    body = base64.b64decode(env.get("body_b64") or "")
    headers = {
        k: v for k, v in (env.get("headers") or [])
        if k.lower() not in _SKIP_REQ_HEADERS
    }
    r = await client.request(env["method"], env["path"], headers=headers, content=body)
    return {
        "t": "res",
        "cid": env["cid"],
        "status": r.status_code,
        # Only content-type matters to claude.ai for JSON responses; forwarding the
        # rest (content-length, encoding) risks mismatching the bytes we send.
        "headers": [["content-type", r.headers.get("content-type", "application/json")]],
        "body_b64": base64.b64encode(r.content).decode(),
    }


def _connect_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if RELAY_DEVICE_TOKEN:
        headers["Authorization"] = f"Bearer {RELAY_DEVICE_TOKEN}"
    # Only the offline mock relay honors this; the real Worker ignores it and routes
    # on the device-JWT `sub`.
    headers["X-Relay-Identity"] = RELAY_IDENTITY
    return headers


async def _serve_connection(client: httpx.AsyncClient) -> None:
    async with websockets.connect(
        RELAY_URL,
        additional_headers=_connect_headers(),
        max_size=MAX_FRAME,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        who = RELAY_IDENTITY if not RELAY_DEVICE_TOKEN else "device-JWT"
        log.info("registered with relay %s (%s)", RELAY_URL, who)
        send_q: asyncio.Queue[str] = asyncio.Queue()

        async def writer() -> None:  # single writer → safe concurrent sends
            while True:
                await ws.send(await send_q.get())

        async def handle(env: dict) -> None:
            try:
                res = await _dispatch(client, env)
            except Exception as e:  # never let one request kill the connection
                log.exception("dispatch failed for cid=%s", env.get("cid"))
                res = {
                    "t": "res", "cid": env.get("cid"), "status": 502,
                    "headers": [["content-type", "application/json"]],
                    "body_b64": base64.b64encode(
                        json.dumps({"error": str(e)}).encode()).decode(),
                }
            await send_q.put(json.dumps(res))

        writer_task = asyncio.create_task(writer())
        try:
            async for raw in ws:
                try:
                    env = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if env.get("t") == "req":
                    asyncio.create_task(handle(env))
        finally:
            writer_task.cancel()


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backoff = RECONNECT_MIN
    # Hold the app lifespan + httpx client open across reconnects so the MCP session
    # manager (and, later, the pre-warmed browser) stays warm — CLAUDE.md lesson #2.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
            while True:
                try:
                    await _serve_connection(client)
                    backoff = RECONNECT_MIN
                except Exception as e:
                    log.warning("relay connection lost (%s); reconnecting in %ss", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
