# Marketplace Finder MCP — agent notes

> This file (and its `AGENTS.md` symlink) is the orientation doc for any future
> session working on this repo. It captures the architecture, the non-obvious
> decisions, and the hard-won debugging lessons so you don't re-discover them.

## What this is

A deliberately **one-job** MCP server: it lets a remote agent (e.g. **claude.ai**)
search **Facebook Marketplace** for an item — anything for sale — through the
**user's own logged-in browser, running on the user's Mac**. The agent gathers
missing details (budget, city, condition) in chat, then calls the search tool.

It was extracted from a sibling project ("hearth", a rental collector) and reuses
that project's proven Playwright scraping approach.

## Architecture (and why)

**Current (relay model — live at `https://mcp.wynnset.com/mcp`):** one shared
Cloudflare Worker + Durable Object is the single public endpoint for *everyone*.
It runs the OAuth authorization server (Google sign-in) and routes each request to
the right person's Mac by **identity**. Each Mac runs an **outbound** agent that
dials the relay over a WebSocket — no inbound tunnel, no public address per Mac.

```
claude.ai ──HTTPS + Bearer──▶ Cloudflare Worker (mcp.<domain>)      [relay/]
                               │  OAuth gate (Google) → route by sub=email
                               ▼  Durable Object per identity
        Alice's Mac ──outbound WSS + device-JWT──▶ DO(alice)         [relay/relay_client.py]
                               │  replays each request into the real FastMCP app
                               ▼
                   logged-in Chromium (Playwright persistent ctx) ──▶ Facebook Marketplace
```

- claude.ai can only reach an MCP server over **HTTP**, and only authenticates via **OAuth**.
- Facebook only shows real results to a **real, logged-in browser** on the user's Mac.
- The Worker terminates claude.ai's TLS + OAuth; the Mac's agent makes an *outbound*
  connection, so the Mac needs no tunnel or open port. The user adds one URL
  (`https://mcp.<domain>/mcp`) to claude.ai and signs in with Google.
- See `docs/RELAY_MIGRATION.md` (design, locked decisions) and `docs/RELAY_DEPLOY.md`
  (deploy runbook). The two meet at `DO(email)`: the access-token `sub` (claude.ai
  side) and the device-JWT `sub` (Mac side) must be the **same email**.

**Historical (pre-relay, superseded):** each Mac ran the FastMCP server locally and
exposed it via its *own* cloudflared named tunnel (`https://mcp.<domain>/mcp` →
tunnel → local server). This is why lessons #1 and #3 below existed; the relay
retired both. `src/server.py serve` still runs that standalone path if ever needed.

## Files

Layout: the importable app lives in **`src/`**; the Cloudflare Worker relay +
Mac-side agent in **`relay/`**; dev/build helpers + test harness in **`scripts/`**;
long-form docs in **`docs/`**. `finder` stays at the repo root (it's the entry
point) and runtime data (`.browser-profile/`, `server.log`, `.geocode-cache.json`,
`logs/`, `.venv/`, `.finder.env`, `.provision/`) lives at the root too —
`src/server.py` resolves `ROOT` to its parent so these paths are unchanged.

| File | Purpose |
|---|---|
| `finder` | **bash CLI** (repo root) that installs/runs/manages the **relay agent** on a Mac: deps, FB login, and the one launchd auto-start LaunchAgent that runs `relay/relay_client.py` (under `caffeinate`). Also `provision <email>` (operator). Start here for anything operational. |
| `relay/src/index.ts` | The **Cloudflare Worker + Durable Object**: OAuth authorization server (built on `@cloudflare/workers-oauth-provider`), `/mcp` resource (routes on verified `ctx.props.sub`), `/agent` device-JWT intake, single-active-agent per identity, KV revocation denylist. |
| `relay/src/google_gate.ts` | The Google sign-in + email-allowlist gate (TS port of `src/oauth.py`'s human gate). |
| `relay/relay_client.py` | The **Mac-side agent**: dials the relay outbound over WSS with a device-JWT, replays each forwarded request into `src/server.py`'s real FastMCP app. Auto-reconnect; holds the lifespan (pre-warmed browser) open across reconnects. |
| `relay/wrangler.jsonc` | Worker config: DO + KV bindings, `PUBLIC_URL`, `custom_domain` route for `mcp.<domain>`. Secrets via `wrangler secret put` (`GOOGLE_*`, `MCP_ALLOWED_EMAILS`, `RELAY_JWT_SECRET`). |
| `src/server.py` | FastMCP server, the two tools, the `login` CLI, and the module-level `app` the relay agent imports. Standalone `serve` still works (legacy/local). |
| `src/devtoken.py` | Mints HS256 device tokens (`finder provision` uses it); the Worker verifies them on `/agent`. |
| `src/oauth.py` | **Legacy** — the in-process OAuth gate for the standalone `serve` path. Superseded by the Worker's OAuth for the relay model; kept for standalone use. |
| `src/gazetteer.py` | **Generated** offline city→(lat,lng) table (~1500: top 1000 US + top 500 CA by population) for the server-side radius filter. Keyed by accent-stripped, `St.`→`Saint`-normalized city name. Committed (it's data). |
| `scripts/build_gaz.py` | Regenerates `src/gazetteer.py` from the GeoNames `cities1000` dump (public domain). |
| `scripts/mock_relay.py` · `scripts/stub_oidc.py` | Offline stand-ins (the Worker+DO; Google's OIDC) for the test harness. |
| `scripts/test_echo.py` · `test_oauth.py` · `test_identity.py` | Relay harness: transport round-trip; OAuth gate (discovery, 401+`WWW-Authenticate`, allowlist); device-JWT identity routing + isolation + revocation. |
| `requirements.txt` | `mcp`, `playwright`, `uvicorn`, `httpx` (httpx is for the Google token exchange + Nominatim geocoding). |
| `docs/RELAY_MIGRATION.md` · `docs/RELAY_DEPLOY.md` | Relay design (locked decisions) + the deploy/cutover runbook. |
| `docs/DEPLOY.md` | The **legacy** cloudflared-tunnel walkthrough (pre-relay). |
| `.browser-profile/` | **gitignored** — the logged-in FB session. Never commit. |
| `.finder.env` | **gitignored** (chmod 600) — `RELAY_URL` + `RELAY_DEVICE_TOKEN` (this Mac's agent creds); on the operator's Mac also `RELAY_JWT_SECRET` + `GOOGLE_*` + `MCP_ALLOWED_EMAILS`. |
| `.provision/` | **gitignored** — per-friend device-token bundles from `finder provision`. Secrets. |
| `logs/` · `server.log` | **gitignored** — launchd stdout/stderr (`logs/agent.*.log`) + the runtime log (`tail -f server.log` to watch searches). |

### The `finder` CLI (the easy path — prefer it over manual steps)

`./finder install` is one idempotent command: venv+deps+Chromium → FB login (if
needed) → require `RELAY_URL` + `RELAY_DEVICE_TOKEN` in `.finder.env` → install the
**one launchd agent** that auto-starts at login and self-heals. Subcommands:
`status` (agent health + connector URL), `logs`, `start`/`stop`/`restart`,
`login` (re-auth FB; stops the agent first so it releases the profile), `url`,
`uninstall`, and `provision <email>` (operator: mint a friend's device-token bundle).

One LaunchAgent lives in `~/Library/LaunchAgents`: `com.wynnset.finder.server`
(label kept for continuity) now runs `caffeinate -is .venv/bin/python
relay/relay_client.py` with `RELAY_URL` + `RELAY_DEVICE_TOKEN` in its env.
`KeepAlive` + `RunAtLoad`; `caffeinate` keeps a closed-lid Mac answering. The CLI
controls it via `launchctl bootstrap/bootout gui/$(id -u) …`. (The old
`com.wynnset.finder.tunnel` cloudflared agent is removed on install/uninstall.)

**Identity match:** the email a friend signs into Google with **must** equal the
email you `provision`ed their device token with, and be on the Worker allowlist
(`MCP_ALLOWED_EMAILS`).

## Tools

- **`search_facebook_marketplace(query, city, min_price, max_price, radius_km,
  near, min_bedrooms, days_listed, sort, max_results, offset)`** → `{query,
  search_url, offset, count, has_more, next_offset, hint, listings[]}`.
  - `radius_km` is enforced via picker + distance filter, NOT the URL (lesson #5);
    `near` centres it on a neighbourhood/landmark; `min_bedrooms` filters cards.
  - Scrapes listing **cards straight off the search results page** (no per-item
    navigation) — fast (~4–5s server-side).
  - **Pagination**: infinite-scroll feed. Pass `offset` to page; the response's
    `has_more`/`next_offset`/`hint` tell the agent to call again for more.
  - Each listing: `{title, price, location, bedrooms, bathrooms, property_type,
    area_sqft, url, photo, raw_text}`. The structured fields may be null;
    `raw_text` (trimmed to 300 chars) is always included so the agent can recover
    anything the heuristic parse missed.
- **`get_listing_details(url)`** → full description/condition for one item.

## Commands

- `python src/server.py login` — one-time interactive (headed) FB login. **Stop
  the server first** — `login` and `serve` both use `.browser-profile/` and can't
  run at once.
- `python src/server.py serve` — run the MCP server (default command).

## Config (env vars, all optional)

`MCP_HOST` (127.0.0.1) · `MCP_PORT` (8000) · `MCP_AUTH_TOKEN` (unset = open) ·
`MCP_ALLOWED_HOSTS` (unset = DNS-rebinding protection OFF) ·
`FB_HEADLESS` (1; set `0` to watch) · `FB_DEFAULT_CITY` (vancouver).

**OAuth gate (the real lock — see "Auth" below):** setting `GOOGLE_CLIENT_ID`
turns it on. `GOOGLE_CLIENT_SECRET` · `MCP_ALLOWED_EMAILS` (comma-separated
allowlist) · `MCP_PUBLIC_URL` (public base URL; auto-derived from
`MCP_ALLOWED_HOSTS` when unset). Precedence: OAuth > `MCP_AUTH_TOKEN` > open.
`./finder install` sets these for you in the launchd plist.

## Run / deploy

**Relay model (current).** Deploy the Worker once, then run the agent per Mac:

```bash
cd relay && npx wrangler deploy            # Worker → mcp.<domain> (see docs/RELAY_DEPLOY.md)
./finder provision <email>                 # operator: mint a friend's device-token bundle
./finder install                           # per Mac: deps, FB login, auto-start agent
# friend adds https://mcp.<domain>/mcp in claude.ai and signs in with Google
```

The Mac's agent (`relay/relay_client.py`) keeps `stateless_http` + `json_response`
and DNS-rebinding protection **OFF** (no public host to pin — the Worker owns that).

**Legacy standalone** (no relay): `python src/server.py serve` + a cloudflared
tunnel — see `docs/DEPLOY.md`. Only relevant if you ever bypass the relay.

---

## ⚠️ HARD-WON LESSONS — read this before debugging "it fails in claude.ai"

The single biggest time-sink was that **claude.ai's failures were almost always
transport problems, not server/Facebook problems.** Confirm the server side first:
`grep "search:" server.log` — if you see `search: N listings in Xs`, the server is
fine and the issue is the tunnel/transport. claude.ai's own error messages blamed
"login walls / CAPTCHA" and these were **wrong** every time.

Lessons #2/#4/#5 are **still live** (in `src/server.py`). Lessons #1 and #3 are
**historical — superseded by the relay** (no per-Mac tunnel/Host anymore), but the
fixes they motivated (`stateless_http` + `json_response`) are still load-bearing and
must not regress — the relay is a pure request/response proxy *because* of them.

1. **[HISTORICAL — relay retired this] DNS-rebinding protection → HTTP 421.** The MCP
   SDK only trusts `localhost` and rejected the tunnel's `Host` header with 421. Fixed
   via `TransportSecuritySettings` (protection OFF; `MCP_ALLOWED_HOSTS` to re-pin). In
   the relay model the Worker terminates claude.ai's Host/TLS and the Mac app has no
   public host, so protection stays OFF on the Mac and there's nothing to pin.

2. **Cold-start timeout.** The first search cold-started Chromium (~seconds) and
   blew past claude.ai's tool wait → cancel. Fixed with a **lifespan pre-warm**
   (`_lifespan` launches the browser at startup) + trimmed page waits. **Still live** —
   `relay_client.py` holds the lifespan open across reconnects so the browser stays warm.

3. **[HISTORICAL — relay retired this] The tunnel was THE recurring failure.** The
   cloudflared quick tunnel used **QUIC**, which dropped and reconnected, killing the
   MCP server's long-lived SSE session stream so claude.ai hung. The relay deletes this
   entire class of bug: the Mac dials *outbound* and every tool call is one independent
   request/response. The two fixes that made that possible are **still load-bearing**:
   - `stateless_http=True` — no persistent session/stream to drop.
   - `json_response=True` — single JSON response, can't be truncated mid-stream.
   - (the old tunnel `--protocol http2` workaround is gone with cloudflared.)

4. **FB DOM is obfuscated** — parsing is heuristic: price by regex
   (`(?:CA)?\$[\d,]+`), title = first non-price line, location = last line.
   `og:image` is often blank on FB → fall back to `alt="Photo of…"` images, then
   the largest `fbcdn.net` image. Expect to tweak `_parse_card` / `SEARCH_CARDS_JS`
   when FB changes their markup.

5. **FB ignores `radius` / `latitude` / `longitude` in the search URL.** Setting
   `&radius=5` does nothing — results span the account's *saved* picker radius
   (was 72 km here → whole Lower Mainland). Proven: `radius=2` vs `radius=100`
   returned the same spread; URL lat/lng returned the account's home city
   (Montréal) instead. So `radius_km` is enforced two ways, neither via URL:
   - **`_set_search_location()`** drives FB's "Change location" dialog (the only
     state FB honours). Setting **both** the location combobox (accepts a
     neighbourhood/landmark via `near`) **and** the radius combobox is what makes
     FB actually tighten — radius-alone stays loose. Side effect: this changes
     the account's global Marketplace location. Fragile (custom comboboxes) →
     best-effort, wrapped in try/except.
   - **server-side distance filter** (`_geocode` + `_haversine_km`) is the
     deterministic backstop. Lookup order: curated `_GAZETTEER` (Metro Van) →
     `gazetteer.py` (~1500 NA cities) → disk cache (`.geocode-cache.json`) →
     Nominatim. Nominatim is throttled to ≤1 req/sec with a contact email
     (`GEOCODE_CONTACT_EMAIL`, per its usage policy) and is rarely hit because
     the gazetteers cover most card cities offline. **City-level granularity
     only**: it drops other cities (Surrey/Abbotsford…) but can't resolve
     sub-city distance — card `location` is just a city name. Applied *before*
     pagination so pages stay coherent; pulls a deeper card pool when filtering.
     Ungeocodable listings are kept, not dropped.

   Cards also carry `bedrooms`/`bathrooms`/`property_type` (and sometimes
   `area_sqft`) — parsed in `_parse_card`; `min_bedrooms` filters on them. sqft is
   usually only on the item page (`get_listing_details`).

## Verifying changes WITHOUT claude.ai

Drive the tools with the MCP Python client against the **local** endpoint (fast,
skips the tunnel):

```python
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client("http://127.0.0.1:8000/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("search_facebook_marketplace",
                                    {"query": "office chair", "city": "vancouver"})
            print(json.loads(res.content[0].text))
asyncio.run(main())
```

Note: in this dev environment, **sandboxed shells can't resolve external
hostnames** (the tunnel host) for DNS — test against `127.0.0.1`, not the public
URL. The public path was validated separately and works.

## Gotchas / future work

- **Python 3.10+** required (`int | None` syntax).
- **Currency**: FB shows local currency (e.g. `CA$` in Vancouver). Price is kept
  as the raw string, not parsed to a number.
- **More marketplaces**: add a new `@mcp.tool()` that builds that site's search
  URL and reuses the card-extraction pattern. Craigslist needs no login but
  blocks datacenter IPs → must be scraped through the same real browser.
## Auth (how the server is locked down)

**Relay model (current):** auth runs in the **Worker**, not on the Mac. The Mac's
agent is reachable *only* through the authenticated relay, so `src/server.py`'s app
runs **open** locally (the relay agent clears `GOOGLE_CLIENT_ID`/`MCP_AUTH_TOKEN`
before importing it). The Worker is the gate: `@cloudflare/workers-oauth-provider`
issues opaque access tokens (claude.ai → `/mcp`), and `relay/src/google_gate.ts` is
the Google sign-in + allowlist gate (TS port of the `src/oauth.py` logic below). The
Mac→`/agent` leg uses a separate HS256 device-JWT (shared `RELAY_JWT_SECRET`), not
OAuth. Token strategy + invariants: `docs/RELAY_MIGRATION.md`. The claude.ai-critical
RFC 9728 `WWW-Authenticate: …resource_metadata=…` header is emitted by the library —
if "Connect" fails with no login screen, check that header on the 401 from `/mcp`.

**Legacy standalone (below) — `src/oauth.py`:** when `GOOGLE_CLIENT_ID` is set on a
standalone `serve`, **`src/oauth.py` runs a minimal OAuth 2.1 authorization server
*inside* this MCP server**, gated by **Google sign-in restricted to
`MCP_ALLOWED_EMAILS`**. The Worker's gate was ported from it; the why is identical. Flow:

```
claude.ai ──/authorize──▶ this server ──redirect──▶ Google sign-in
          ◀──token──────  ◀──/oauth/google/callback (verify email ∈ allowlist)
```

The MCP SDK (`auth_server_provider=` + `AuthSettings`) provides the
/authorize·/token·/register·/revoke endpoints, PKCE verification, and the
metadata docs; `src/oauth.py` supplies storage + the Google email gate. Tokens +
DCR clients persist to `.oauth-store.json` so launchd restarts don't force a
reconnect.

**Why this design and not the obvious alternatives — don't regress these:**
- **A static bearer token (`MCP_AUTH_TOKEN`) cannot lock down claude.ai.** Its
  custom-connector UI sends no custom header; it only speaks OAuth. The token
  gate still exists for *other* clients, but OAuth takes precedence.
- **Cloudflare Access is broken for claude.ai web/mobile** (anthropics/
  claude-ai-mcp#410, closed "not planned"): Access's "Managed OAuth" 401 omits
  the RFC 9728 `WWW-Authenticate: Bearer resource_metadata="…"` header that
  claude.ai requires (Claude Code tolerates its absence; claude.ai does not).
  Server-native OAuth works precisely because the SDK **emits that header** — if
  you ever see claude.ai fail at "Connect" with no login screen, check that the
  401 from `/mcp` still carries `WWW-Authenticate` with `resource_metadata`.
- Google's `redirect_uri` (`https://<host>/oauth/google/callback`) must match
  the one registered in the Google Cloud OAuth client **exactly**, and is built
  once in `src/oauth.py` (`self.redirect_uri`) for both the authorize and token legs.

## Gotchas / future work (auth)

- Verifying the OAuth flow can't go through the live `streamablehttp_client`
  (no browser to complete Google login). Drive the provider directly instead:
  construct `GoogleOAuthProvider`, stub `_fetch_email`, and exercise
  `authorize()` → `google_callback()` → `/token`. (The SDK plumbing — DCR, PKCE,
  refresh rotation, bearer 401 — is identical regardless of the email gate.)
- **More allowed users**: add their emails to `MCP_ALLOWED_EMAILS`. Revoke by
  removing the email (and optionally deleting their tokens from
  `.oauth-store.json`).
