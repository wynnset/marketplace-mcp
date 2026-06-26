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

It walks through five steps and is **safe to re-run** (it skips anything already
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
4. **Access control (Google sign-in)** — recommended. Locks the server to your
   Google account so only you can drive your logged-in Facebook session. See
   "Lock it down with Google sign-in" below for the one-time Google Cloud setup;
   answer `n` to skip and run open (secured only by the secret URL).
5. **Auto-start services** — installs two launchd agents (server + tunnel) that
   start now, **start again at every login**, and **restart on crash**.

### Lock it down with Google sign-in (recommended)

Without a gate, anyone who learns your `https://mcp.<domain>/mcp` URL can run
searches through *your* logged-in Facebook session. The installer can require a
**Google sign-in** restricted to emails you list. A static token won't do —
claude.ai's connector sends no custom header — and Cloudflare Access is broken
for claude.ai web (see CLAUDE.md → "Auth"); brokering Google OAuth in the server
is the path that actually works.

One-time setup in
[Google Cloud → APIs & Services](https://console.cloud.google.com/apis/credentials),
two parts:

**A. Configure the OAuth consent screen** (do this first — Google requires it
before it'll issue a client ID):

1. **APIs & Services → OAuth consent screen.**
2. **User type:**
   - Choose **External** if you want **anyone (incl. `@gmail.com` friends)** to be
     able to sign in. This is the right choice for sharing — see "Adding friends"
     below.
   - Choose **Internal** only if everyone who'll use it is in your own Google
     Workspace org (e.g. all `@wynnset.com`). It's slightly cleaner but rejects
     outside accounts.
3. Fill **App name**, **User support email**, **Developer contact email**.
   Under **Authorized domains** add your domain (e.g. `wynnset.com`).
4. **Scopes:** you can leave them empty or add `openid` + `…/auth/userinfo.email`.
   These are **non-sensitive**, so Google requires **no app verification**.
5. If you picked **External**, click **Publish app → Confirm** to move it from
   "Testing" to **Production**. (In Testing you'd have to add each user as a
   "test user" and they'd see an "unverified app" warning. Production with only
   non-sensitive scopes is instant — no review.)

**B. Create the OAuth client ID:**

1. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
2. **Application type: Web application.**
3. Under **Authorized redirect URIs**, add **exactly**:
   `https://<your-host>/oauth/google/callback`
   (e.g. `https://mcp.wynnset.com/oauth/google/callback`). It must match your
   tunnel hostname character-for-character, no trailing slash.
4. Click **Create**, then copy the **Client ID** and **Client secret**.
5. Run `./finder install`; at the access-control step paste the Client ID and
   secret, and enter the allowed email(s) (comma-separated).

These are saved to `.finder.env` (chmod 600) and injected into the server's
launchd plist. When you add the connector in claude.ai you'll get a Google
sign-in; only the allowlisted account(s) can connect. Verify the mode anytime
with `./finder status` (look for "access: Google sign-in").

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
   (the `/mcp` path is required).
4. Save. If Google sign-in is on, a **Google sign-in window pops up** — sign in
   with an allowlisted account. claude.ai then discovers the two tools.
5. In a chat, make sure the connector is **enabled** (toggle near the message box).

> **Saw "Server not found" right after adding it?** That's a known transient —
> claude.ai sometimes shows it while the OAuth round-trip (register → Google
> sign-in → token) is still finishing. **Refresh the page / reopen the connector**
> and it'll be connected. It is not a real error; your server log will show the
> `/register → /authorize → /token → /mcp 200` sequence completing.

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

### Adding & removing friends (who can sign in)

Access is controlled by the **email allowlist**, `MCP_ALLOWED_EMAILS`. To let a
friend in (any Google account, once your consent screen is **External +
Production**):

1. Edit `.finder.env` and add their email to the comma-separated list:
   ```
   MCP_ALLOWED_EMAILS="aidin@wynnset.com,friend@gmail.com"
   ```
2. `./finder install` (re-run — it re-injects the new value and restarts the
   service). Or edit the launchd plist value directly and `./finder restart`.
3. Send your friend the connector URL (`https://mcp.<domain>/mcp`). They add it
   in their own claude.ai and sign in with that Google account.

**To revoke someone:** remove their email from the list and `./finder restart`.
To also kill any token they already hold, delete `.oauth-store.json` (everyone
re-authenticates next time — harmless) and restart.

> Heads-up: every friend's searches run through **your** logged-in Facebook
> account on **your** Mac (one shared browser). Fine for a handful of trusted
> people; Facebook sees all of it as your activity. Heavy simultaneous use would
> need a browser pool (not built yet).

### Keep the project folder put

The launchd services point at this folder's absolute path. **Don't move or
rename the folder** while the service is configured — if you do, re-run
`./finder install` from the new location so the plists are rewritten (otherwise
the auto-start service points at a path that no longer exists). Check what the
live service actually runs with `./finder status`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **"Server not found" right after adding the connector** | Transient — the OAuth round-trip is still finishing. **Refresh / reopen the connector.** Confirm via `./finder logs`: you'll see `/register → /authorize → /token → /mcp 200`. |
| Google sign-in shows **"redirect_uri_mismatch"** | The redirect URI in the Google Cloud client must be **exactly** `https://<your-host>/oauth/google/callback` (no trailing slash, https, exact host). Fix it in Credentials → your client. |
| Sign-in says **"this account is not authorized"** | The email isn't in `MCP_ALLOWED_EMAILS`. Add it and `./finder restart` (see "Adding & removing friends"). |
| Google **"Access blocked: app not verified"** / asks to add test users | Consent screen is still in **Testing**. Set it to **External + Publish to Production** (non-sensitive scopes need no review). |
| claude.ai connects but **stalls at "Connect"** with no Google popup | Check the 401 from `/mcp` still carries the `WWW-Authenticate … resource_metadata` header (`curl -i https://<host>/mcp` via POST). That header is what claude.ai needs; see CLAUDE.md → "Auth". |
| Search returns "login wall" / no results | Session expired → `./finder login`. |
| `./finder status` shows server DOWN | `./finder logs` to see why; `./finder restart`. |
| Public endpoint not reachable on first setup | Give DNS ~60s, re-check `./finder status`. Confirm the CNAME exists in Cloudflare. |
| claude.ai can't connect | Is the public endpoint reachable in `./finder status`? Did you include `/mcp`? |
| "browser not found" / Playwright error | `.venv/bin/python -m playwright install chromium`. |
| Connector option missing in claude.ai | Custom connectors require a paid plan. |
| Want to watch the browser work | It runs headless as a service. To watch, `./finder stop` then `FB_HEADLESS=0 .venv/bin/python src/server.py serve` in a terminal. |
| Zero results but you expect some | Try a different `city` slug, widen the price range, or set `sort: newest`. |

---

## Appendix: the manual path (no installer)

If you'd rather run things by hand (or debug), the pieces underneath are:

```bash
# one-time
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python src/server.py login

# permanent named tunnel (one-time)
cloudflared tunnel login
cloudflared tunnel create marketplace-mcp
cloudflared tunnel route dns marketplace-mcp mcp.yourdomain.com
# write ~/.cloudflared/marketplace-mcp.yml pointing the hostname at http://localhost:8000

# run (two processes)
MCP_ALLOWED_HOSTS=mcp.yourdomain.com python src/server.py serve
cloudflared tunnel --config ~/.cloudflared/marketplace-mcp.yml run
```

`./finder install` just automates all of the above and wraps the two run-processes
in launchd so they survive logout/crash. A throwaway **quick tunnel**
(`cloudflared tunnel --protocol http2 --url http://localhost:8000`) still works for
a one-off test, but its URL rotates on every restart — that's the thing the named
tunnel fixes.
