# Relay migration — one URL for every friend's Mac

> Working plan for moving from **per-friend Cloudflare subdomains** to a **single
> shared endpoint** (`https://mcp.<domain>/mcp`) where claude.ai authenticates via
> Google sign-in and requests are routed to the right person's Mac by **identity**.
> Read this top-to-bottom before continuing; the decisions below are **locked** —
> implement them, don't re-litigate them.

## Why

Today each friend needs their own subdomain + Cloudflare tunnel (see `finder`'s
`ensure_tunnel`). Goal: **one** connector URL everyone adds to claude.ai, with
OAuth identity deciding which Mac serves the request — no per-friend subdomain, no
per-friend Cloudflare provisioning, and the Macs need no public address at all.

## Locked decisions

| Decision | Choice |
|---|---|
| Replace subdomain routing with… | a **Cloudflare Worker + Durable Object** relay |
| OAuth identity | **Google sign-in**, thin self-host (allowlist of friends' emails) |
| Where the OAuth authorization server runs | **inside the Worker (TS)** — ported from `src/oauth.py` |
| Access token format | **signed JWT** (Worker verifies signature + claims, routes on `sub`) |
| Token signing | **HS256 with a shared secret** `RELAY_JWT_SECRET` (Worker + `finder` both know it). RS256/JWKS is the cleaner-separation alternative if we ever split issuers. |
| Routing key | the verified **`sub` = email** (lowercased). *Future hardening: Google `sub` via trust-on-first-use; see "Later".* |

## Target architecture

```
                    ┌──────────── Cloudflare Worker (mcp.<domain>) ────────────┐
                    │ AUTH SERVER (ported from src/oauth.py → TS)              │
claude.ai ──────────┤  /.well-known/oauth-authorization-server                 │
  discover + login  │  /authorize → Google (openid email) → /oauth/google/cb   │
                    │     verify email_verified + allowlist → mint signed JWT   │
                    │  /token (PKCE) · /register (DCR) · /revoke                │
                    │ RESOURCE SERVER                                           │
claude.ai ──/mcp───▶│  /.well-known/oauth-protected-resource → (AS above)       │
  + Bearer JWT      │  verify JWT(sig,iss,aud,exp) → sub=email → DO(email)       │
                    │ AGENT INTAKE                                              │
                    │  /agent (WSS) verify device-JWT(sub=email) → DO(email)     │
                    └───────────────────────────┬──────────────────────────────┘
                                                 ▼  Durable Object per identity
Alice's Mac ── outbound WSS + device-JWT(alice) ── register at DO(alice)
   (no cloudflared, no public address) ── serves the real FB MCP tools
   ── logged-in Chromium ── Facebook
```

The two legs meet at `DO(email)`: the access-token `sub` (claude.ai side) and the
device-JWT `sub` (Mac side) must be the **same email** — that's the routing key.

## Security invariants — DO NOT violate

Spoofing ("use someone else's identity to reach their Mac") is prevented *by
construction* only if all of these hold:

1. **Route on a signed claim, never a client-supplied string.** The DO id is
   derived inside the Worker from the *verified* `sub`. There is no identity header
   a caller can set (the Step-1 `X-Relay-Identity` stub is removed in Step 3).
2. **Verify every token fully**: signature (pinned alg — reject `alg:none`), `iss`,
   `aud` == this relay, and `exp`. Applies to both access tokens and device tokens.
3. **Access tokens ≠ device tokens.** Distinguish them with a claim (e.g.
   `aud:"relay-mcp"` vs `aud:"relay-agent"` or a `typ`), so an access token can't
   register an agent and a device token can't call `/mcp`.
4. **Identity only ever comes from Google**, with `email_verified === true` and the
   email on the allowlist (this is already correct in `src/oauth.py`).
5. **TLS everywhere** (Cloudflare gives this) and the device token is a **secret**
   (treat like a password; make it revocable — KV denylist by `jti`/email).

---

## Status

- ✅ **Step 1 — transport skeleton. DONE & PROVEN.**
  - `relay/src/index.ts` — Worker + `RelayDO`: `/agent` WS intake, `/mcp` proxy with
    per-request correlation ids, single-active-agent per DO. Typechecks clean.
  - `relay/relay_client.py` — Mac side: dials out, registers, serves a real
    in-process FastMCP `echo` tool by replaying each forwarded request through the
    SDK (lifespan + `httpx.ASGITransport`); auto-reconnect + single-writer queue.
  - `scripts/mock_relay.py` + `scripts/test_echo.py` — offline E2E harness.
  - Proven: `python scripts/test_echo.py` PASSES against both the mock **and** the
    real Worker under `wrangler dev`; a second identity with no agent returns 503
    (per-identity DO isolation). Stateless `tools/call` works as an independent POST
    → the relay is a clean request/response proxy, retiring cloudflared lesson #3.
  - Stubbed: identity = `X-Relay-Identity` header; **no auth yet**.

---

## Step 2 — OAuth authorization server in the Worker (signed JWTs)

**Goal:** claude.ai connects to `https://mcp.<domain>/mcp`, gets bounced to Google,
signs in with an allowlisted email, and reaches the (still echo) tool — all auth
handled by the Worker.

**Do:**
- Port the gate from `src/oauth.py` to TS in `relay/`. **Strongly prefer building
  on `@cloudflare/workers-oauth-provider`** — it implements DCR, PKCE, the
  `/authorize` `/token` `/register` `/revoke` endpoints, the metadata documents,
  **and the RFC 9728 `WWW-Authenticate: Bearer resource_metadata="…"` header that
  claude.ai requires** (this exact header is why server-native OAuth works where
  Cloudflare Access failed — see `src/oauth.py` header + CLAUDE.md "Auth"). You only
  supply the human gate.
- The gate (1:1 with `oauth.py`): `/authorize` → redirect to Google
  (`scope=openid email`, `prompt=select_account`); `/oauth/google/callback` →
  exchange code with `GOOGLE_CLIENT_SECRET`, read userinfo, **require
  `email_verified` and email ∈ allowlist**, else deny.
- On approval, mint a **signed JWT access token**: claims `sub`=email (lowercased),
  `iss`=public URL, `aud`="relay-mcp", `exp` (~1 day), `scope`. Sign HS256 with
  `RELAY_JWT_SECRET`. Issue a refresh token (rotate on use) for claude.ai.
- Serve both `/.well-known/oauth-authorization-server` and
  `/.well-known/oauth-protected-resource` (resource = `…/mcp`).
- Storage (DCR clients, refresh tokens, pending `state`, used-code guard, JWKS if
  RS256): a **KV namespace** (add to `wrangler.jsonc`).
- Config as Worker vars/secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `MCP_ALLOWED_EMAILS`, `RELAY_JWT_SECRET`, `PUBLIC_URL`. Google's redirect URI
  (`https://mcp.<domain>/oauth/google/callback`) must be registered **exactly** in
  the Google Cloud OAuth client.

**Verify (no browser needed for the gate):** unit-test that `/mcp` with no/invalid
token → 401 **with** the `WWW-Authenticate` header; with a hand-minted valid JWT →
reaches echo. Drive the Google leg manually once in a browser end-to-end.

---

## Step 3 — JWT identity routing + device tokens

**Goal:** identity comes only from verified tokens; alice can't reach bob's Mac.

**Do:**
- `/mcp`: remove the `X-Relay-Identity` stub. Verify the Bearer JWT (sig, `iss`,
  `aud`=="relay-mcp", `exp`); route to `DO(sub)`. Invalid → 401 (+ `WWW-Authenticate`).
- `/agent`: verify a **device-JWT** (sig, `aud`=="relay-agent", `sub`=email; long or
  no `exp`); route to `DO(sub)`. Invalid → 401/refused upgrade.
- Keep access ≠ agent token separation (invariant #3). Keep single-active-agent.
- Add a **KV revocation denylist** (by `jti` or email) checked on both legs.

**Verify:** extend `scripts/test_echo.py` (or add a TS/wrangler test) — a token for
identity A never reaches an agent registered as B; a revoked token is refused;
`/agent` rejects an access token and `/mcp` rejects a device token.

---

## Step 4 — port the real Facebook tools onto the relay transport

**Goal:** real searches run through the relay.

**Do:**
- Change `relay/relay_client.py` to serve the **real** app from `src/server.py`
  (the `mcp` with `search_facebook_marketplace` + `get_listing_details` and the
  Chromium pre-warm lifespan) instead of its local echo app.
- Make `src/server.py` import-safe: expose the FastMCP app object without starting
  uvicorn and **without the OAuth gate** (OAuth now lives in the Worker; the Mac app
  is reachable *only* through the authenticated relay, so it runs "open" locally).
  Keep `stateless_http` + `json_response` and rebinding protection **OFF** on the Mac
  (no public host to pin). Keep the lifespan pre-warm (lesson #2) — `relay_client`
  already holds the lifespan open across reconnects.

**Verify:** with a logged-in `.browser-profile`, a real `search_facebook_marketplace`
round-trips through `wrangler dev` + `relay_client.py`; `grep "search:" server.log`
shows the server side succeeding.

---

## Step 5 — provisioning, installer, and removing cloudflared

**Goal:** a non-technical friend goes from a notarized double-click to a working
connector, and `finder` no longer does per-friend tunnels.

**Do:**
- `finder provision <email>`: mint a device-JWT(`sub`=email, `aud`="relay-agent")
  signed with `RELAY_JWT_SECRET`; add the email to the Worker allowlist
  (`MCP_ALLOWED_EMAILS` secret or KV); emit the friend's installer bundle with
  `RELAY_URL` + the device token baked in.
- Gut cloudflared from `finder`: remove `ensure_tunnel`, the
  `com.wynnset.finder.tunnel` LaunchAgent, `MCP_ALLOWED_HOSTS`, `--protocol http2`.
  The **server LaunchAgent now runs `relay/relay_client.py`** (env: `RELAY_URL`,
  `RELAY_DEVICE_TOKEN`) instead of `src/server.py serve`. Consider `caffeinate` so a
  closed-lid Mac still answers, and document "works while your Mac is awake + online".
- Build the **notarized `.app`/`.pkg`** (Apple Developer account is available):
  bundle/auto-fetch a portable Python + Chromium (no Homebrew/git/Terminal),
  run the headed FB login once, install the one LaunchAgent, then show the friend
  the connector URL + "add it in claude.ai and sign in with Google".
- **Friend UX:** double-click installer → log into Facebook once → in claude.ai add
  `https://mcp.<domain>/mcp` → Google sign-in. Done.

**Verify:** a fresh Mac (or a clean user account) goes installer → working search in
claude.ai with zero Terminal use.

---

## Cross-cutting / housekeeping

- **Identity match:** the email a friend uses for Google sign-in **must** equal the
  email you `provision`ed their device token with. Document this; the allowlist
  enforces it.
- **Update `CLAUDE.md`** after cutover: new architecture diagram, `relay/` files,
  and demote the cloudflared lessons (#1, #3) to "historical — superseded by the
  relay". Keep #2 (cold-start pre-warm) and #4/#5 (FB DOM / radius) — still live.
- **Don't regress the FB scraping** (`src/server.py`, gazetteer radius filter).

## Later (optional hardening, not blocking)

- Route on Google **`sub`** instead of email (bind `sub` on first sign-in, TOFU),
  to neutralize email-reassignment edge cases.
- DO **hibernation** for the agent WebSocket (cheaper idle); Step 1 holds it in
  memory for simplicity.
- Cloudflare Access in front of `/agent` as defense-in-depth on device tokens.
