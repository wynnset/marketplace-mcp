#!/usr/bin/env python3
"""
test_echo.py — end-to-end proof of the relay transport (no auth; mock relay).

Drives the official MCP streamable-HTTP client against the relay's /mcp endpoint
(default: the local mock_relay) and asserts the real Facebook tools are advertised
through the reverse tunnel (Step 4 swapped the Step-1 echo tool for the real app):

    this client → relay → /agent WebSocket → relay_client.py → FastMCP app → back

By default it boots the mock relay + relay_client itself, so `python scripts/
test_echo.py` is a single self-contained check. Point it at a real Worker with:

    RELAY_HTTP=https://mcp.<domain> RELAY_WS=wss://mcp.<domain>/agent \
        python scripts/test_echo.py --no-spawn
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

RELAY_HTTP = os.environ.get("RELAY_HTTP", "http://127.0.0.1:8787")
RELAY_WS = os.environ.get("RELAY_WS", "ws://127.0.0.1:8787/agent")
IDENTITY = os.environ.get("RELAY_IDENTITY", "default")


async def _wait_port(host: str, port: int, timeout: float = 10.0) -> None:
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


async def run_client() -> int:
    headers = {"X-Relay-Identity": IDENTITY}
    async with streamablehttp_client(f"{RELAY_HTTP}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print(f"  tools/list → {names}")
            # Step 4: the relay client now serves the real Facebook app, so the
            # transport proof is that those tools round-trip through the relay.
            assert "search_facebook_marketplace" in names, \
                "Facebook tools not advertised through the relay"
    print("\n✅ PASS — claude.ai → relay → DO/agent → Mac → tools/list round-trips")
    return 0


async def main() -> int:
    spawn = "--no-spawn" not in sys.argv
    procs = []
    if spawn:
        env = {**os.environ, "RELAY_URL": RELAY_WS, "RELAY_IDENTITY": IDENTITY}
        py = sys.executable
        print("· starting mock relay on :8787")
        procs.append(await asyncio.create_subprocess_exec(
            py, "-m", "uvicorn", "scripts.mock_relay:app", "--port", "8787",
            "--log-level", "warning"))
        await _wait_port("127.0.0.1", 8787)
        print("· starting relay/relay_client.py (echo agent)")
        relay_client = os.path.join(os.path.dirname(__file__), "..", "relay", "relay_client.py")
        procs.append(await asyncio.create_subprocess_exec(py, relay_client, env=env))
        await asyncio.sleep(1.5)  # let the agent register its WS

    try:
        return await run_client()
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
