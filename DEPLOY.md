# Deploy & use — every step from zero to "Claude, search Marketplace for me"

This is the full linear sequence. By the end you'll be able to message claude.ai
*"find me a used Aeron chair under $400 in Seattle"* and have it search Facebook
Marketplace in your own logged-in browser.

There are **two terminal windows that must stay running** while you use this:
one for the server, one for the tunnel. That's normal — close them and the
connector goes offline until you start them again (see [Daily use](#8-daily-use--restarting)).

---

## 0. Prerequisites (one-time)

- **macOS** with Python **3.10+** (you have 3.14 — fine).
- A **claude.ai paid plan** (Pro / Max / Team / Enterprise). Custom connectors
  are not available on the free tier.
- **Homebrew** (you have it) — used to install the tunnel tool.

---

## 1. Install the app (one-time, ~2 min + a browser download)

```bash
cd marketplace-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium      # downloads a private Chromium (~150 MB)
```

> If you already ran these during setup, you can skip — but re-run
> `python -m playwright install chromium` if you ever see a "browser not found" error.

---

## 2. Log into Facebook (one-time, until the session expires)

```bash
. .venv/bin/activate          # if not already active
python server.py login
```

A real Chrome window opens. **Log into Facebook normally** (handle any 2FA),
make sure you land on your logged-in home feed, then **close the window**.
The session is saved to `.browser-profile/` and reused by the server.

You only redo this if Facebook later logs you out (the search tool will tell you
when that happens).

---

## 3. Start the server (keep this terminal open)

```bash
. .venv/bin/activate
python server.py serve
```

You should see:

```
Auth: OPEN (no MCP_AUTH_TOKEN set) — keep your tunnel URL private
Marketplace Finder MCP on http://127.0.0.1:8000/mcp
```

Leave this running. Confirm it's alive from **another** terminal:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/mcp
# a number like 400/406 means "listening" (it rejects a bare GET — that's expected)
# "connection refused" means it isn't running
```

*(Optional, best local test)* — drive the tools with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector
# open the URL it prints, set Transport = "Streamable HTTP",
# URL = http://127.0.0.1:8000/mcp, Connect, then run search_facebook_marketplace
```

---

## 4. Install the tunnel tool (one-time)

claude.ai lives in the cloud and can't reach `localhost`. A tunnel gives your
local server a public HTTPS URL. `cloudflared` needs no account and is simplest:

```bash
brew install cloudflared
```

---

## 5. Start the tunnel (keep this terminal open too)

In a **new** terminal (leave the server running in the other one):

```bash
cloudflared tunnel --protocol http2 --url http://localhost:8000
```

It prints a line like:

```
https://random-words-here.trycloudflare.com
```

That's your public base URL. **Your MCP endpoint is that URL + `/mcp`:**

```
https://random-words-here.trycloudflare.com/mcp
```

> ⚠️ A quick tunnel gets a **new random URL every time you restart it**. Fine for
> personal use — you just re-paste the new URL into claude.ai when it changes.
> For a permanent URL see [Stable URL](#stable-url-optional) below.

---

## 6. Add it to claude.ai (re-do only when the URL changes)

1. Go to **claude.ai → Settings → Connectors** (a.k.a. "Custom connectors").
2. Click **Add custom connector**.
3. **Name:** `Marketplace Finder`
4. **URL:** paste your endpoint, e.g.
   `https://random-words-here.trycloudflare.com/mcp`
5. Save. claude.ai connects and discovers the two tools
   (`search_facebook_marketplace`, `get_listing_details`).
6. In a chat, make sure the connector is **enabled** for that conversation
   (the connectors/tools toggle near the message box).

> Leave `MCP_AUTH_TOKEN` unset for this flow — the custom-connector UI connects
> without a bearer header, so the endpoint must be open. Your protection is the
> unguessable tunnel URL. (To lock it down properly, use a named tunnel behind
> Cloudflare Access — out of scope here.)

---

## 7. Use it 🎉

In that chat, just ask in plain language. Examples:

- *"Search Facebook Marketplace for a used Herman Miller Aeron chair under $400 in Seattle, listed in the last week."*
- *"Find me a Yeti Tundra 45 cooler near Vancouver, newest first, show me 15."*
- *"Look for a 27-inch 4K monitor under $250 in NYC, then give me the full details on the cheapest one."*

Claude will ask you for anything it's missing (usually **budget** and **city**),
then call the tool. The search runs in your logged-in Chromium and returns
titles, prices, locations, photos, and links.

---

## 8. Daily use / restarting

Once installed, the recurring routine is just:

```bash
# terminal 1
cd marketplace-mcp && . .venv/bin/activate && python server.py serve

# terminal 2
cloudflared tunnel --protocol http2 --url http://localhost:8000
```

- If the tunnel URL changed, update the connector URL in claude.ai (step 6).
- If Facebook logged you out, re-run `python server.py login` (step 2).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Search returns "login wall" / no results | Session expired → `python server.py login` again. |
| claude.ai can't connect to the connector | Is `cloudflared` still running? Did the URL change? Is the server up (`curl` test in step 3)? Did you include `/mcp` at the end? |
| "browser not found" / Playwright error | `python -m playwright install chromium`. |
| Connector option missing in claude.ai | Custom connectors require a paid plan. |
| Want to watch the browser work | Start the server with `FB_HEADLESS=0 python server.py serve`. |
| Zero results but you expect some | Try a different `city` slug (`seattle`, `nyc`, `la`, `chicago`), widen the price range, or set `sort: newest`. |

---

## Stable URL (optional)

A quick tunnel's URL changes on every restart. For a permanent endpoint, create a
**named** Cloudflare tunnel bound to a domain you control:

```bash
cloudflared tunnel login
cloudflared tunnel create marketplace-mcp
cloudflared tunnel route dns marketplace-mcp mcp.yourdomain.com
cloudflared tunnel run --url http://localhost:8000 marketplace-mcp
# endpoint becomes:  https://mcp.yourdomain.com/mcp  (stable forever)
```

Then you paste that URL into claude.ai once and never touch it again.
