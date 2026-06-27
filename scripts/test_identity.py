#!/usr/bin/env python3
"""
test_identity.py — Step-3 verification: identity comes only from verified tokens,
and alice can't reach bob's Mac.

Drives a running `wrangler dev` (start it yourself; see scripts/test_oauth.py header)
and reuses that module's OAuth helpers to obtain a real opaque access token. Device
tokens are minted here as HS256 JWTs (no PyJWT dependency) using RELAY_JWT_SECRET,
which must match relay/.dev.vars.

Proves:
  1. /agent refuses an upgrade with no token, a bad signature, wrong `aud`, `alg:none`,
     or an opaque access token (access ≠ device tokens — invariant #3, one direction)
  2. a valid device-JWT registers and its `sub` routes /mcp to that Mac (alice→alice)
  3. ISOLATION: with only bob's agent connected, alice's access token → 503, never bob
  4. /mcp refuses a device-JWT presented as a Bearer (invariant #3, other direction)
  5. a revoked device token (KV denylist) is refused the upgrade

Run (spawns the stub OIDC + agents itself; --no-spawn-stub to manage the stub):
    cd relay && npx wrangler dev --port 8787      # terminal 1
    .venv/bin/python scripts/test_identity.py     # terminal 2
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys

import httpx
import websockets

sys.path.insert(0, os.path.dirname(__file__))
from test_oauth import (  # noqa: E402  (reuse the Step-2 OAuth helpers + device signer)
    ALLOWED_EMAIL,
    RELAY_HTTP,
    RELAY_WS,
    STUB_PORT,
    _wait_port,
    device_jwt,
    reach_relay_tools,
    register_client,
    run_flow,
)

ROOT = os.path.join(os.path.dirname(__file__), "..")
RELAY_CLIENT = os.path.join(ROOT, "relay", "relay_client.py")
# A valid device identity that is NOT on the email allowlist. Unique per run so the
# revocation check below (which writes a DURABLE denylist key to the local KV) can't
# bleed into the next run and refuse a fresh bob.
BOB_EMAIL = f"bob-{secrets.token_hex(4)}@example.com"


# ── helpers ────────────────────────────────────────────────────────────────────
async def agent_upgrade_status(headers: dict[str, str]):
    """Attempt a raw /agent WS upgrade. Return None if it CONNECTED, else the status code."""
    try:
        async with websockets.connect(RELAY_WS, additional_headers=headers, open_timeout=10):
            return None
    except Exception as e:  # websockets raises InvalidStatus(.response.status_code)
        return (
            getattr(getattr(e, "response", None), "status_code", None)
            or getattr(e, "status_code", None)
            or "rejected"
        )


async def start_agent(device_token: str) -> asyncio.subprocess.Process:
    """Start relay_client.py with a device-JWT and wait until it registers."""
    env = {**os.environ, "RELAY_URL": RELAY_WS, "RELAY_DEVICE_TOKEN": device_token}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, RELAY_CLIENT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)  # type: ignore[union-attr]
        if not line:
            raise RuntimeError("agent exited before registering")
        if b"registered with relay" in line:
            break
    else:
        raise TimeoutError("agent did not register in time")
    # Drain remaining output so the pipe never blocks the agent mid-test.
    asyncio.create_task(_drain(proc))
    return proc


async def _drain(proc: asyncio.subprocess.Process) -> None:
    try:
        while True:
            if not await proc.stdout.readline():  # type: ignore[union-attr]
                return
    except Exception:
        return


async def stop(proc: asyncio.subprocess.Process) -> None:
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()


async def kv_revoke(key: str) -> bool:
    """Add a denylist key to the local RELAY_KV that `wrangler dev` reads."""
    proc = await asyncio.create_subprocess_exec(
        "npx", "wrangler", "kv", "key", "put", key, "1",
        "--binding", "RELAY_KV", "--local",
        cwd=os.path.join(ROOT, "relay"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        print("  ! wrangler kv put failed:\n" + out.decode(errors="replace"))
    return proc.returncode == 0


async def post_mcp(token: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=20) as c:
        return await c.post(
            f"{RELAY_HTTP}/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )


# ── the checks ───────────────────────────────────────────────────────────────────
async def run_checks() -> int:
    # An allowlisted access token (alice), obtained through the real OAuth flow.
    async with httpx.AsyncClient(follow_redirects=False, timeout=20) as c:
        client_id = await register_client(c)
        flow = await run_flow(c, client_id, "code-allowed")
        assert "token" in flow, f"could not obtain alice access token: {flow}"
    alice_access = flow["token"]
    alice_dev = device_jwt(ALLOWED_EMAIL, jti="alice-jti-1")

    # 1. /agent refuses everything that isn't a valid device-JWT.
    cases = {
        "no token": {},
        "opaque access token": {"Authorization": f"Bearer {alice_access}"},
        "bad signature": {"Authorization": f"Bearer {device_jwt(ALLOWED_EMAIL, secret='wrong-secret')}"},
        "wrong aud": {"Authorization": f"Bearer {device_jwt(ALLOWED_EMAIL, aud='relay-mcp')}"},
        "alg:none": {"Authorization": f"Bearer {device_jwt(ALLOWED_EMAIL, alg='none')}"},
    }
    for name, headers in cases.items():
        status = await agent_upgrade_status(headers)
        assert status == 401, f"/agent should reject {name!r} with 401, got {status}"
    print("  ✓ /agent refuses: no token, opaque access token, bad sig, wrong aud, alg:none")

    # 2. valid device-JWT registers and routes /mcp to that identity (alice → alice).
    agent = await start_agent(alice_dev)
    try:
        await reach_relay_tools(alice_access)
    finally:
        await stop(agent)
    print("  ✓ valid device-JWT registers; alice's access token reaches alice's agent (real FB tools)")

    # 3. ISOLATION: only bob's agent is connected → alice's access token gets no agent.
    bob_dev = device_jwt(BOB_EMAIL, jti="bob-jti-1")
    agent = await start_agent(bob_dev)
    try:
        r = await post_mcp(alice_access)
        assert r.status_code == 503, f"alice→/mcp with only bob online should be 503, got {r.status_code}"
        assert "agent offline" in r.text, f"unexpected 503 body: {r.text!r}"
    finally:
        await stop(agent)
    print("  ✓ isolation: alice's token routes to DO(alice), never reaches bob's agent")

    # 4. /mcp refuses a device-JWT presented as a Bearer (not a library token).
    r = await post_mcp(alice_dev)
    assert r.status_code == 401, f"/mcp should reject a device-JWT with 401, got {r.status_code}"
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
    print("  ✓ /mcp refuses a device-JWT (401 + WWW-Authenticate)")

    # 5. revocation: deny bob by email, then his device token can't open /agent.
    if await kv_revoke(f"revoked:sub:{BOB_EMAIL}"):
        status = await agent_upgrade_status({"Authorization": f"Bearer {bob_dev}"})
        assert status == 401, f"revoked device token should be refused 401, got {status}"
        print("  ✓ revoked device token (KV denylist) is refused the upgrade")
    else:
        print("  ! SKIPPED revocation E2E (could not write local KV); verify manually")
        return 1

    print("\n✅ PASS — Step 3 identity routing: device-JWT gate, cross-identity isolation, "
          "token-type separation, revocation")
    return 0


async def main() -> int:
    spawn_stub = "--no-spawn-stub" not in sys.argv
    procs = []
    if spawn_stub:
        print(f"· starting stub OIDC on :{STUB_PORT}")
        procs.append(await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uvicorn", "scripts.stub_oidc:app", "--port", str(STUB_PORT),
            "--log-level", "warning", cwd=ROOT))
        await _wait_port("127.0.0.1", STUB_PORT)
    try:
        return await run_checks()
    finally:
        for p in procs:
            await stop(p)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
