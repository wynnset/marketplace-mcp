# Deploy & use — from zero to "Claude, search Marketplace for me"

By the end you'll be able to message claude.ai *"find me a used Aeron chair under
$400 in Seattle"* and have it search Facebook Marketplace in your own logged-in
browser — over a **permanent URL** that **auto-starts at login**. No terminals to
keep open, no URL to re-paste.

---

## The fast path: `./finder install`

### 0. Prerequisites (one-time)

- **macOS** with Python **3.10+**.
- A **claude.ai paid plan** (Pro / Max / Team / Enterprise) — custom connectors
  aren't on the free tier.
- **Homebrew**, and `cloudflared`:  `brew install cloudflared`
- A **domain you manage in Cloudflare DNS** (e.g. `wynnset.com`). The permanent
  endpoint will be a subdomain of it, like `mcp.wynnset.com`. If your domain
  isn't on Cloudflare yet, add it (free) at dash.cloudflare.com first.

### 1. Run the installer

```bash
cd marketplace-mcp
./finder install
```

It walks through four steps and is **safe to re-run** (it skips anything already
done):

1. **Python environment** — creates `.venv`, installs deps, downloads Chromium
   (~150 MB, first time only).
2. **Facebook session** — if you've never logged in, it opens a real Chrome
   window. **Log into Facebook normally** (handle 2FA), land on your home feed,
   then **close the window**. Saved to `.browser-profile/`.
3. **Cloudflare tunnel** — asks for your hostname (default `mcp.wynnset.com`),
   then:
   - opens a browser to **authorize cloudflared** — pick the domain that owns
     that hostname and click **Authorize** (one time);
   - creates a named tunnel `marketplace-mcp`;
   - writes its ingress config and **routes DNS** so `mcp.<domain>` → the tunnel.
4. **Auto-start services** — installs two launchd agents (server + tunnel) that
   start now, **start again at every login**, and **restart on crash**.

When it finishes it prints your permanent URL:

```
https://mcp.<your-domain>/mcp
```

### 2. Verify

```bash
./finder status
```

You want to see the local server **up**, both services **loaded**, and the public
endpoint **reachable** (DNS can take ~30–60s the very first time).

### 3. Add it to claude.ai — once, forever

1. **claude.ai → Settings → Connectors → Add custom connector.**
2. **Name:** `Marketplace Finder`
3. **URL:** your permanent endpoint, e.g. `https://mcp.wynnset.com/mcp`
4. Save. claude.ai discovers the two tools.
5. In a chat, make sure the connector is **enabled** (toggle near the message box).

Because the URL never changes, you do this **once** and never again.

### 4. Use it 🎉

Just ask in plain language:

- *"Search Facebook Marketplace for a used Herman Miller Aeron chair under $400 in Seattle, listed in the last week."*
- *"Find me a Yeti Tundra 45 cooler near Vancouver, newest first, show me 15."*
- *"Look for a 27-inch 4K monitor under $250 in NYC, then give me the full details on the cheapest one."*

Claude asks for anything it's missing (usually **budget** and **city**), then runs
the search in your logged-in Chromium.

---

## Day-to-day

It's hands-off — the services run on their own. The `finder` CLI is there when you
need it:

| Command | What it does |
|---|---|
| `./finder status` | server + tunnel health, plus your connector URL |
| `./finder logs` | tail the live server + tunnel logs |
| `./finder restart` | bounce both services |
| `./finder stop` / `start` | stop / start both services |
| `./finder login` | re-log into Facebook when the session expires |
| `./finder url` | print the connector URL |
| `./finder uninstall` | remove the auto-start services (keeps code + login + tunnel) |

**If Facebook logs you out**, the search tool says so — run `./finder login`
(it stops the server, opens the headed login, then restarts).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Search returns "login wall" / no results | Session expired → `./finder login`. |
| `./finder status` shows server DOWN | `./finder logs` to see why; `./finder restart`. |
| Public endpoint not reachable on first setup | Give DNS ~60s, re-check `./finder status`. Confirm the CNAME exists in Cloudflare. |
| claude.ai can't connect | Is the public endpoint reachable in `./finder status`? Did you include `/mcp`? |
| "browser not found" / Playwright error | `.venv/bin/python -m playwright install chromium`. |
| Connector option missing in claude.ai | Custom connectors require a paid plan. |
| Want to watch the browser work | It runs headless as a service. To watch, `./finder stop` then `FB_HEADLESS=0 .venv/bin/python server.py serve` in a terminal. |
| Zero results but you expect some | Try a different `city` slug, widen the price range, or set `sort: newest`. |

---

## Appendix: the manual path (no installer)

If you'd rather run things by hand (or debug), the pieces underneath are:

```bash
# one-time
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python server.py login

# permanent named tunnel (one-time)
cloudflared tunnel login
cloudflared tunnel create marketplace-mcp
cloudflared tunnel route dns marketplace-mcp mcp.yourdomain.com
# write ~/.cloudflared/marketplace-mcp.yml pointing the hostname at http://localhost:8000

# run (two processes)
MCP_ALLOWED_HOSTS=mcp.yourdomain.com python server.py serve
cloudflared tunnel --config ~/.cloudflared/marketplace-mcp.yml run
```

`./finder install` just automates all of the above and wraps the two run-processes
in launchd so they survive logout/crash. A throwaway **quick tunnel**
(`cloudflared tunnel --protocol http2 --url http://localhost:8000`) still works for
a one-off test, but its URL rotates on every restart — that's the thing the named
tunnel fixes.
