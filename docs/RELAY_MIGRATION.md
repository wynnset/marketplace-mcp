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
| Where the OAuth authorization server runs | **inside the Worker (TS)**, built on **`@cloudflare/workers-oauth-provider`** (DCR, PKCE, metadata docs, the RFC 9728 `WWW-Authenticate` header) — we only supply the Google sign-in + allowlist gate (ported from `src/oauth.py`). |
| Access token format (claude.ai → `/mcp`) | **opaque, issued by `@cloudflare/workers-oauth-provider`** — KV-backed grant; the Worker reads the verified identity from `ctx.props.sub`. *Not a JWT* (see "Token strategy" note below). |
| Device token format (Mac → `/agent`) | **signed JWT** — `finder` mints it offline; the Worker verifies signature + claims and routes on `sub`. |
| Token signing (device tokens only) | **HS256 with a shared secret** `RELAY_JWT_SECRET` (Worker + `finder` both know it). RS256/JWKS is the cleaner-separation alternative if we ever split issuers. |
| Routing key | the verified **`sub` = email** (lowercased) — from `ctx.props.sub` on `/mcp`, from the device-JWT `sub` on `/agent`. *Future hardening: Google `sub` via trust-on-first-use; see "Later".* |

> **Token strategy (revised — was "signed JWT for both legs").** The two legs use
> *different* token mechanisms on purpose. The **device-token** leg genuinely needs a
> signed JWT: `finder` mints it offline and bakes it into the friend's installer, so
> the Worker must verify it statelessly with the shared secret — nothing is pre-stored.
> The **access-token** leg is a closed loop (the Worker both issues and verifies it via
> `@cloudflare/workers-oauth-provider`), so opaque KV-backed tokens are simpler *and
> slightly safer there*: instant revocation (delete the grant), no `RELAY_JWT_SECRET`
> blast-radius on that path, and the library encrypts `props` with the token. We let
> the library own the claude.ai-facing flow precisely because it emits the RFC 9728
> `WWW-Authenticate` header claude.ai requires — the riskiest thing to hand-roll.
> Security invariant #1 (route on a Worker-verified claim, never a client string) holds
> for both: `ctx.props.sub` on `/mcp`, device-JWT `sub` on `/agent`.

## Target architecture

```
                    ┌──────────── Cloudflare Worker (mcp.<domain>) ────────────┐
                    │ AUTH SERVER (@cloudflare/workers-oauth-provider + gate)   │
claude.ai ──────────┤  /.well-known/oauth-authorization-server                 │
  discover + login  │  /authorize → Google (openid email) → /oauth/google/cb   │
                    │   verify email_verified + allowlist → completeAuth(props) │
                    │  /token (PKCE) · /register (DCR) · /revoke   [library]     │
                    │ RESOURCE SERVER                                           │
claude.ai ──/mcp───▶│  /.well-known/oauth-protected-resource → (AS above)       │
  + Bearer (opaque) │  library validates token → ctx.props.sub=email → DO(email)│
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

1. **Route on a verified claim, never a client-supplied string.** The DO id is
   derived inside the Worker from the *verified* identity — `ctx.props.sub` on `/mcp`
   (set from Google's verified email at authorization time), the device-JWT `sub` on
   `/agent`. There is no identity header a caller can set (the Step-1
   `X-Relay-Identity` stub is removed in Step 3).
2. **Verify every token fully.** *Access tokens (`/mcp`):* validated by
   `@cloudflare/workers-oauth-provider` against its KV grant store — an unknown,
   expired, or revoked token never reaches the API handler. *Device tokens
   (`/agent`):* verify the JWT signature (pinned alg — reject `alg:none`),
   `aud == "relay-agent"`, `sub`, and `exp`.
3. **Access tokens ≠ device tokens — separated by construction.** They are different
   formats verified on different paths: an opaque library token can't pass JWT
   verification on `/agent`, and a device JWT isn't in the library's KV grant store so
   it can't authenticate `/mcp`. (We still set `aud == "relay-agent"` on device tokens
   as a belt-and-suspenders discriminator.)
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
- Build on **`@cloudflare/workers-oauth-provider`** — it implements DCR, PKCE, the
  `/authorize` `/token` `/register` `/revoke` endpoints, the metadata documents,
  **and the RFC 9728 `WWW-Authenticate: Bearer resource_metadata="…"` header that
  claude.ai requires** (this exact header is why server-native OAuth works where
  Cloudflare Access failed — see `src/oauth.py` header + CLAUDE.md "Auth"). It issues
  **opaque, KV-backed access tokens** — we do NOT mint JWTs on the claude.ai leg. You
  only supply the human gate.
- Wire it up: `apiRoute = "/mcp"` + an `apiHandler` that reads identity from
  `ctx.props.sub` and proxies into `DO(sub)`; a `defaultHandler` serving the Google
  gate (`/authorize`, `/oauth/google/callback`) plus the non-OAuth routes (`/agent`,
  `/healthz`). The library serves both well-known metadata docs and `/token`,
  `/register`, `/revoke` itself.
- The gate (1:1 with `oauth.py`): `/authorize` → `parseAuthRequest`, stash the
  request keyed by a random `state` in `RELAY_KV`, redirect to Google
  (`scope=openid email`, `prompt=select_account`); `/oauth/google/callback` →
  exchange code with `GOOGLE_CLIENT_SECRET`, read userinfo, **require
  `email_verified` and email ∈ allowlist**, else deny. On approval call
  `completeAuthorization({ request, userId: email, scope, props: { sub: email } })`
  and redirect to the returned `redirectTo`. The library mints the opaque token,
  stores the grant in KV, and handles `/token` + refresh rotation.
- Storage: a KV namespace bound as **`OAUTH_KV`** (the library's grant/client/token
  store) and a second **`RELAY_KV`** for our own state (the in-flight Google `state`,
  and the Step-3 device-token revocation denylist). Add both to `wrangler.jsonc`.
- Config as Worker vars/secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `MCP_ALLOWED_EMAILS`, `PUBLIC_URL` (and `RELAY_JWT_SECRET`, used from Step 3 for
  device tokens — not needed for the access leg). Google's redirect URI
  (`https://mcp.<domain>/oauth/google/callback`) must be registered **exactly** in
  the Google Cloud OAuth client.

**Verify (no browser needed):** (1) `/mcp` with no/invalid token → 401 **with** the
`WWW-Authenticate: Bearer resource_metadata="…"` header, and both well-known docs
serve. (2) With `GOOGLE_*` pointed at a stub OIDC endpoint, run the whole flow over
HTTP against `wrangler dev` — DCR `/register` → `/authorize` → `/oauth/google/callback`
(stub returns an allowlisted, `email_verified` account) → `/token` (PKCE) → call `/mcp`
with the issued opaque token and reach echo; a non-allowlisted email is denied. Drive
the real Google leg manually in a browser once.

---

## Step 3 — JWT identity routing + device tokens

**Goal:** identity comes only from verified tokens; alice can't reach bob's Mac.

**Do:**
- `/mcp`: identity already comes from the library's verified `ctx.props.sub` (done in
  Step 2 — the `X-Relay-Identity` stub is gone on this leg). Nothing further here.
- `/agent`: remove the `X-Relay-Identity` stub. Verify a **device-JWT** (sig with
  `RELAY_JWT_SECRET`, pinned alg, `aud`=="relay-agent", `sub`=email; long or no
  `exp`); route to `DO(sub)`. Invalid → refused upgrade (401/426).
- Access ≠ agent token separation holds by construction (invariant #3): an opaque
  access token can't pass `/agent`'s JWT check, and a device JWT isn't in `OAUTH_KV`
  so it can't authenticate `/mcp`. Keep single-active-agent.
- Add a **KV revocation denylist** in `RELAY_KV` (by `jti` or email) checked on the
  `/agent` leg; access tokens are revoked by deleting the library grant.

**Verify:** extend `scripts/test_echo.py` (or add a TS/wrangler test) — a device token
for identity A never reaches a `/mcp` caller authorized as B; a revoked device token is
refused the upgrade; `/agent` rejects an access (opaque) token and `/mcp` rejects a
device JWT.

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
