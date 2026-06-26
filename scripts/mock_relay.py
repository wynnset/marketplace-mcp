#!/usr/bin/env python3
"""
mock_relay.py — a local stand-in for the Cloudflare Worker + Durable Object.

It speaks the exact same wire protocol as relay/src/index.ts (the `req`/`res`
envelopes over a WebSocket at /agent, and POST /mcp), so you can prove the whole
transport on one machine with no Cloudflare account or network:

    claude.ai client  ──POST /mcp──▶  mock_relay  ──WS /agent──▶  relay_client.py

Routing is by the `X-Relay-Identity` header, mirroring the Worker's stub. This is
test scaffolding only; the real relay is the Worker. Run via scripts/test_echo.py
or manually (uvicorn scripts.mock_relay:app --port 8787).
"""
from __future__ import annotations

import asyncio
import base64
import itertools

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


class _Identity:
    """One identity == one connected agent + its in-flight requests (mirrors a DO)."""

    def __init__(self) -> None:
        self.agent: WebSocket | None = None
        self.send_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.seq = itertools.count()


_identities: dict[str, _Identity] = {}


def _slot(identity: str) -> _Identity:
    return _identities.setdefault(identity, _Identity())


async def healthz(_req: Request) -> PlainTextResponse:
    return PlainTextResponse("ok\n")


async def agent_ws(ws: WebSocket) -> None:
    identity = ws.headers.get("x-relay-identity", "default")
    slot = _slot(identity)
    await ws.accept()
    if slot.agent is not None:
        try:
            await slot.agent.close(code=1012)
        except Exception:
            pass
    slot.agent = ws
    try:
        while True:
            env = await ws.receive_json()
            if env.get("t") != "res":
                continue
            fut = slot.pending.pop(env.get("cid", ""), None)
            if fut and not fut.done():
                fut.set_result(env)
    except WebSocketDisconnect:
        pass
    finally:
        if slot.agent is ws:
            slot.agent = None


async def mcp(req: Request) -> Response:
    identity = req.headers.get("x-relay-identity", "default")
    slot = _slot(identity)
    if slot.agent is None:
        return Response(
            '{"jsonrpc":"2.0","error":{"code":-32001,"message":"agent offline"},"id":null}',
            status_code=503, media_type="application/json")

    cid = str(next(slot.seq))
    body = await req.body()
    envelope = {
        "t": "req", "cid": cid, "method": req.method, "path": "/mcp",
        "headers": [[k, v] for k, v in req.headers.items() if k.lower() != "x-relay-identity"],
        "body_b64": base64.b64encode(body).decode(),
    }
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    slot.pending[cid] = fut
    try:
        async with slot.send_lock:
            await slot.agent.send_json(envelope)
        res = await asyncio.wait_for(fut, timeout=30)
    except (asyncio.TimeoutError, Exception) as e:
        slot.pending.pop(cid, None)
        return Response(
            f'{{"jsonrpc":"2.0","error":{{"code":-32002,"message":"{e}"}},"id":null}}',
            status_code=504, media_type="application/json")

    headers = {k: v for k, v in res.get("headers", [])}
    return Response(base64.b64decode(res["body_b64"]),
                    status_code=res["status"],
                    media_type=headers.get("content-type", "application/json"))


app = Starlette(routes=[
    Route("/healthz", healthz),
    Route("/mcp", mcp, methods=["POST"]),
    WebSocketRoute("/agent", agent_ws),
])
