# Marketplace Finder — an MCP server that searches marketplaces in your own browser

One job: let a remote agent (e.g. **claude.ai**) search online marketplaces for
an item — anything for sale, not just housing — through **your** logged-in
browser, running on **your** machine.

## How it fits together

```
claude.ai  ──HTTPS──▶  tunnel (cloudflared/ngrok)  ──▶  this server (your Mac)
                                                            │
                                                            ▼
                                              your logged-in Chromium (Playwright)
                                                            │
                                                            ▼
                                                   Facebook Marketplace
```

claude.ai can only reach an MCP server over HTTP, but Facebook Marketplace only
shows real results to a real, logged-in browser. So this server runs **locally**
(Streamable-HTTP transport), drives your persistent logged-in Chromium, and you
expose it to claude.ai through a tunnel.

The agent gathers missing details from you in chat (budget, city, condition…)
before it calls a tool — the tools just declare parameters.

## Tools the agent gets

- **`search_facebook_marketplace`** — `query`, `city`, `min_price`, `max_price`,
  `radius_km`, `days_listed`, `sort` (`best`/`newest`/`price_low`/`price_high`/`distance`),
  `max_results`. Returns structured listings (title, price, location, url, photo,
  raw text) pulled straight off the results page.
- **`get_listing_details`** — full description / condition for one listing URL.

## Setup

> **Just want the full walkthrough?** See **[DEPLOY.md](DEPLOY.md)** — every step
> from zero to asking claude.ai to search Marketplace for you (install, login,
> tunnel, claude.ai connector, daily restart routine, troubleshooting).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python server.py login     # one-time: sign into Facebook, then close the window
python server.py serve      # starts the MCP server on http://localhost:8000/mcp
```

Then expose it and register it with claude.ai:

```bash
cloudflared tunnel --protocol http2 --url http://localhost:8000
#   (or:  ngrok http 8000)
```

In **claude.ai → Settings → Connectors → Add custom connector**, paste the
tunnel URL with the MCP path:

```
https://<your-tunnel-host>/mcp
```

Now ask claude.ai things like *"find me a used Herman Miller Aeron under $400 in
Seattle, listed this week."* It will ask for anything it's missing, then call the
search tool, which runs in your browser.

## Config (environment variables, all optional)

| Var | Default | Meaning |
|-----|---------|---------|
| `MCP_HOST` | `127.0.0.1` | bind address |
| `MCP_PORT` | `8000` | port |
| `MCP_AUTH_TOKEN` | _(unset)_ | if set, require `Authorization: Bearer <token>` |
| `MCP_ALLOWED_HOSTS` | _(unset)_ | comma-separated Host allow-list. Unset = DNS-rebinding protection off (needed so a rotating tunnel host isn't rejected with HTTP 421). Set it to pin specific hosts. |
| `FB_HEADLESS` | `1` | set `0` to watch the browser while serving |
| `FB_DEFAULT_CITY` | `vancouver` | FB city slug used when a search omits one |

If `MCP_AUTH_TOKEN` is unset the endpoint is open — keep your tunnel URL private,
or set a token. (claude.ai connectors that support a bearer/OAuth secret can pass it.)

## Notes & next steps

- **Facebook city slugs** are short names like `vancouver`, `seattle`, `nyc`,
  `la`, `chicago`. The agent passes one as `city`.
- **Session expiry:** if Facebook starts showing a login wall, the tool says so
  — re-run `python server.py login`.
- **Adding marketplaces** (Craigslist, eBay, Kijiji…): add a new `@mcp.tool()`
  in `server.py` that builds that site's search URL and reuses the same
  card-extraction pattern. Craigslist needs no login; it's scraped through the
  same real browser because it blocks datacenter IPs.
- **Clarifying questions** are handled by the agent today. If you later want the
  *server* to drive structured prompts, that's MCP "elicitation" — a tool can
  request input mid-call via `ctx.elicit(...)`; client support is still uneven.

This folder is self-contained and ready to move into its own repo.
