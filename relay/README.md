# marketplace-relay — Step 1 skeleton (transport proof)

One public URL for every friend's Mac. Requests are routed to the right Mac by
**identity**, not by a per-friend subdomain:

```
claude.ai ──POST /mcp──▶ Worker ─▶ DO(identity) ─┐
Alice Mac ──WSS /agent─▶ Worker ─▶ DO(identity) ◀┘  (holds the live socket)
```

A Durable Object is created per identity (`idFromName(identity)`); it's where
claude.ai's request meets the Mac's registered WebSocket. Every MCP tool call is a
single JSON response (the Mac runs `stateless_http` + `json_response`), so the DO
is a plain request/response proxy with correlation ids — no stream to keep alive,
and none of cloudflared's QUIC/SSE drops (CLAUDE.md lesson #3).

## What's in this step

| Piece | File | Role |
|---|---|---|
| Worker + DO | `relay/src/index.ts` | the real relay: `/agent` WS intake, `/mcp` proxy |
| Mac client | `relay/relay_client.py` | dials out, registers, serves a FastMCP `echo` tool over the socket |
| Mock relay | `../scripts/mock_relay.py` | Python stand-in for the Worker+DO (same wire protocol) for offline tests |
| E2E test | `../scripts/test_echo.py` | drives the official MCP client through the relay |

**Stubbed for now (Steps 2–3):** identity is the `X-Relay-Identity` header. There
is no auth yet. Step 2 adds the OAuth authorization server (ported from `oauth.py`)
issuing signed JWTs; Step 3 derives identity from the verified token `sub` (`/mcp`)
and a signed device-JWT `sub` (`/agent`).

## Test it locally (no Cloudflare account needed)

Self-contained — boots the mock relay + agent and round-trips `echo`:

```bash
.venv/bin/python scripts/test_echo.py
# ✅ PASS — claude.ai → relay → DO/agent → Mac → echo round-trips
```

## Test against the real Worker (local workerd)

```bash
cd relay && npm install
npx wrangler dev --port 8788 --local          # terminal 1

RELAY_URL=ws://127.0.0.1:8788/agent python relay_client.py    # terminal 2

RELAY_HTTP=http://127.0.0.1:8788 \
  python ../scripts/test_echo.py --no-spawn   # terminal 3
```

## Deploy (Step 5 will automate this)

```bash
cd relay
npm install
npx wrangler deploy
# then route mcp.<domain> → this Worker (custom domain in wrangler.jsonc / dash)
```

`npm run typecheck` runs `tsc --noEmit`.
