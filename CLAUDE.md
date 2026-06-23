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

```
claude.ai ──HTTPS──▶ cloudflared tunnel ──▶ local MCP server (this, on the Mac)
                                                  │
                                                  ▼
                                   logged-in Chromium (Playwright persistent ctx)
                                                  │
                                                  ▼
                                         Facebook Marketplace
```

- claude.ai can only reach an MCP server over **HTTP**.
- Facebook only shows real results to a **real, logged-in browser**.
- So the server runs **locally** (Streamable-HTTP transport) and is exposed to
  claude.ai via a **tunnel**. The user adds `https://<tunnel-host>/mcp` as a
  claude.ai custom connector.

## Files

| File | Purpose |
|---|---|
| `finder` | **bash CLI** that installs/runs/manages everything: deps, FB login, the permanent Cloudflare named tunnel, and the launchd auto-start services. Start here for anything operational. |
| `server.py` | Everything else: FastMCP server, the two tools, and the `login` CLI. |
| `requirements.txt` | `mcp`, `playwright`, `uvicorn`. |
| `README.md` | Short overview + setup. |
| `DEPLOY.md` | Full step-by-step deploy/use walkthrough + troubleshooting. |
| `.browser-profile/` | **gitignored** — the logged-in FB session. Never commit. |
| `.finder.env` | **gitignored** — remembers your tunnel hostname (`FINDER_HOSTNAME`). |
| `logs/` | **gitignored** — launchd stdout/stderr for the server + tunnel services. |
| `server.log` | **gitignored** — runtime log (`tail -f` it to watch searches). |

### The `finder` CLI (the easy path — prefer it over manual steps)

`./finder install` is one idempotent command: venv+deps+Chromium → FB login (if
needed) → **named Cloudflare tunnel** routed to `mcp.<domain>` (permanent URL) →
**launchd services** that auto-start at login and self-heal. Other subcommands:
`status` (health + the connector URL), `logs`, `start`/`stop`/`restart`,
`login` (re-auth FB; stops the server first so it releases the profile),
`url`, `uninstall`.

Two LaunchAgents live in `~/Library/LaunchAgents`:
`com.wynnset.finder.server` (runs `.venv/bin/python server.py serve`) and
`com.wynnset.finder.tunnel` (runs `cloudflared tunnel --config
~/.cloudflared/marketplace-mcp.yml run`). Both `KeepAlive` + `RunAtLoad`; the CLI
controls them via `launchctl bootstrap/bootout gui/$(id -u) …`.

**Permanent host ⇒ DNS-rebinding protection is back ON.** The server LaunchAgent
sets `MCP_ALLOWED_HOSTS=<your hostname>`, so the SDK pins the Host header. This is
only possible because the named tunnel's host is stable — the old quick-tunnel URL
rotated, which is why protection had to be off (see lesson #1 below).

## Tools

- **`search_facebook_marketplace(query, city, min_price, max_price, radius_km,
  days_listed, sort, max_results, offset)`** → `{query, search_url, offset,
  count, has_more, next_offset, hint, listings[]}`.
  - Scrapes listing **cards straight off the search results page** (no per-item
    navigation) — fast (~4–5s server-side).
  - **Pagination**: infinite-scroll feed. Pass `offset` to page; the response's
    `has_more`/`next_offset`/`hint` tell the agent to call again for more.
  - Each listing: `{title, price, location, url, photo, raw_text}`. `raw_text`
    (trimmed to 300 chars) is always included so the agent can recover anything
    the heuristic parse missed.
- **`get_listing_details(url)`** → full description/condition for one item.

## Commands

- `python server.py login` — one-time interactive (headed) FB login. **Stop the
  server first** — `login` and `serve` both use `.browser-profile/` and can't run
  at once.
- `python server.py serve` — run the MCP server (default command).

## Config (env vars, all optional)

`MCP_HOST` (127.0.0.1) · `MCP_PORT` (8000) · `MCP_AUTH_TOKEN` (unset = open) ·
`MCP_ALLOWED_HOSTS` (unset = DNS-rebinding protection OFF) ·
`FB_HEADLESS` (1; set `0` to watch) · `FB_DEFAULT_CITY` (vancouver).

## Run / deploy

```bash
python server.py serve
cloudflared tunnel --protocol http2 --url http://localhost:8000   # note: http2!
# add https://<tunnel-host>/mcp as a claude.ai custom connector
```

Quick-tunnel URLs **rotate on every restart** → re-paste into claude.ai each time.
For a permanent URL, use a named Cloudflare tunnel + your own domain (see
DEPLOY.md → "Stable URL").

---

## ⚠️ HARD-WON LESSONS — read this before debugging "it fails in claude.ai"

The single biggest time-sink was that **claude.ai's failures were almost always
transport problems, not server/Facebook problems.** Confirm the server side first:
`grep "search:" server.log` — if you see `search: N listings in Xs`, the server is
fine and the issue is the tunnel/transport. claude.ai's own error messages blamed
"login walls / CAPTCHA" and these were **wrong** every time.

The four fixes, all currently in `server.py` / the run command — do **not** regress them:

1. **DNS-rebinding protection → HTTP 421.** The MCP SDK only trusts `localhost`
   by default and rejects the tunnel's `Host` header with 421. Fixed via
   `TransportSecuritySettings` (default: protection OFF; set `MCP_ALLOWED_HOSTS`
   to re-pin). See the `_security` block.

2. **Cold-start timeout.** The first search cold-started Chromium (~seconds) and
   blew past claude.ai's tool wait → cancel. Fixed with a **lifespan pre-warm**
   (`_lifespan` launches the browser at startup) + trimmed page waits.

3. **The tunnel was THE recurring failure.** The Cloudflare quick tunnel uses
   **QUIC**, which kept dropping (`no recent network activity`) and reconnecting;
   the MCP server's **long-lived SSE session stream** died on each reconnect, so
   claude.ai hung waiting (up to its 5-minute timeout). Tunnel-log tell:
   `stream canceled by remote with error code 0` = the client gave up. Fixed with
   **all three** together:
   - `stateless_http=True` — no persistent session/stream to drop.
   - `json_response=True` — single JSON response, can't be truncated mid-stream.
   - tunnel `--protocol http2` — eliminates the QUIC idle timeouts.

4. **FB DOM is obfuscated** — parsing is heuristic: price by regex
   (`(?:CA)?\$[\d,]+`), title = first non-price line, location = last line.
   `og:image` is often blank on FB → fall back to `alt="Photo of…"` images, then
   the largest `fbcdn.net` image. Expect to tweak `_parse_card` / `SEARCH_CARDS_JS`
   when FB changes their markup.

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
- **Auth**: claude.ai's custom-connector UI connects without a custom header, so
  the endpoint runs open and the secret is the tunnel URL. For real lockdown, use
  a named tunnel behind Cloudflare Access.
