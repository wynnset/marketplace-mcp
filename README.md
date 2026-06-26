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

## Setup — one command

> **Want the step-by-step + troubleshooting?** See **[DEPLOY.md](docs/DEPLOY.md)**.

```bash
brew install cloudflared          # if you don't have it yet
./finder install
```

`./finder install` does everything, idempotently:

1. creates the virtualenv, installs deps, downloads Chromium,
2. opens a browser for your one-time **Facebook login**,
3. sets up a **permanent Cloudflare tunnel** to `https://mcp.<your-domain>/mcp`
   (a named tunnel routed to a domain you manage in Cloudflare — the URL never
   changes), and
4. installs **launchd services** so the server + tunnel **auto-start at login**
   and restart themselves if they crash. No terminals to babysit.

When it finishes it prints your permanent connector URL. Paste it **once** into
**claude.ai → Settings → Connectors → Add custom connector**:

```
https://mcp.<your-domain>/mcp
```

Now ask claude.ai things like *"find me a used Herman Miller Aeron under $400 in
Seattle, listed this week."* It will ask for anything it's missing, then call the
search tool, which runs in your browser.

### Managing it

```bash
./finder status      # health of server + tunnel, and your connector URL
./finder logs        # tail the live logs
./finder restart     # bounce both services
./finder login       # re-log into Facebook when the session expires
./finder stop        # /start, /uninstall also available
```

Because the tunnel host is now **stable**, the server pins it via
`MCP_ALLOWED_HOSTS` — DNS-rebinding protection is back **on** (the old rotating
quick-tunnel URL couldn't do this).

## Config (environment variables, all optional)

| Var | Default | Meaning |
|-----|---------|---------|
| `MCP_HOST` | `127.0.0.1` | bind address |
| `MCP_PORT` | `8000` | port |
| `GOOGLE_CLIENT_ID` | _(unset)_ | set it to **lock the server with Google sign-in** (the recommended gate that works with claude.ai). |
| `GOOGLE_CLIENT_SECRET` | _(unset)_ | the matching Google OAuth client secret |
| `MCP_ALLOWED_EMAILS` | _(unset)_ | comma-separated allowlist of Google emails permitted to connect |
| `MCP_PUBLIC_URL` | _(auto)_ | public base URL for OAuth metadata + the Google redirect; derived from your tunnel host when unset |
| `MCP_AUTH_TOKEN` | _(unset)_ | static `Authorization: Bearer <token>` — works for non-claude.ai clients only (claude.ai sends no custom header). Ignored when OAuth is on. |
| `MCP_ALLOWED_HOSTS` | _(unset)_ | comma-separated Host allow-list. Unset = DNS-rebinding protection off (needed so a rotating tunnel host isn't rejected with HTTP 421). Set it to pin specific hosts. |
| `FB_HEADLESS` | `1` | set `0` to watch the browser while serving |
| `FB_DEFAULT_CITY` | `vancouver` | FB city slug used when a search omits one |

### Locking it down (recommended)

Without a gate, **anyone who learns your URL can drive your logged-in Facebook
session.** `./finder install` offers to lock the server to your **Google
account**. One-time setup in
[Google Cloud](https://console.cloud.google.com/apis/credentials) — full
step-by-step (incl. the consent screen) is in **[DEPLOY.md](docs/DEPLOY.md)**:

1. **OAuth consent screen** → User type **External**, then **Publish to
   Production** (our scopes are non-sensitive, so no Google review). External +
   Production is what lets friends with any Gmail sign in.
2. **Credentials → OAuth client ID**, type **Web application**, with the
   **Authorized redirect URI** exactly `https://<your-host>/oauth/google/callback`.
3. Run `./finder install`; paste the **Client ID** + **secret** and list the
   allowed email(s).

claude.ai then shows a Google sign-in when you add the connector; only the
allowlisted account(s) get in. (Cloudflare Access does **not** work here — it
breaks claude.ai web; see CLAUDE.md → "Auth".)

**Sharing with friends:** add their Gmail to `MCP_ALLOWED_EMAILS` in `.finder.env`
and `./finder restart`, then send them the URL — they add it in their own
claude.ai and sign in. Remove the email + restart to revoke. (Their searches run
through *your* Facebook session on *your* Mac.) Details in docs/DEPLOY.md →
"Adding & removing friends".

> First connect shows **"Server not found"**? It's a transient while the OAuth
> handshake finishes — just refresh. See docs/DEPLOY.md → Troubleshooting.

## Notes & next steps

- **Facebook city slugs** are short names like `vancouver`, `seattle`, `nyc`,
  `la`, `chicago`. The agent passes one as `city`.
- **Session expiry:** if Facebook starts showing a login wall, the tool says so
  — run `./finder login` (re-auths FB, then restarts the service).
- **Adding marketplaces** (Craigslist, eBay, Kijiji…): add a new `@mcp.tool()`
  in `src/server.py` that builds that site's search URL and reuses the same
  card-extraction pattern. Craigslist needs no login; it's scraped through the
  same real browser because it blocks datacenter IPs.
- **Clarifying questions** are handled by the agent today. If you later want the
  *server* to drive structured prompts, that's MCP "elicitation" — a tool can
  request input mid-call via `ctx.elicit(...)`; client support is still uneven.

This folder is self-contained and ready to move into its own repo.
