#!/usr/bin/env python3
"""
Finder — a one-job MCP server: let a remote agent (e.g. claude.ai) search
online marketplaces for an item, through your own logged-in browser.

Why this shape:
  - claude.ai can only reach an MCP server over HTTP.
  - Facebook Marketplace only shows real results to a real, logged-in browser.
So the server runs LOCALLY on your Mac (Streamable-HTTP transport), drives a
persistent logged-in Chromium via Playwright, and you expose it to claude.ai
through a tunnel (cloudflared / ngrok). claude.ai calls the tools; the scraping
happens in your session, on your machine.

The agent gathers missing details (budget, city, condition) from you in chat
before it calls a tool — the tools just declare parameters. No marketplace
login flow lives in the agent; you do that once, by hand, with `login`.

Commands:
    python server.py login     # one-time: open a browser, log into Facebook
    python server.py serve      # run the MCP server (default)

Setup (once):
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    python -m playwright install chromium
    python server.py login
    python server.py serve
    # then expose it:  cloudflared tunnel --url http://localhost:8000
    # add  https://<tunnel-host>/mcp  as a custom connector in claude.ai

Config is via environment variables (all optional):
    MCP_HOST          bind address           (default 127.0.0.1)
    MCP_PORT          port                   (default 8000)
    MCP_AUTH_TOKEN    require Bearer token   (default: none / open)
    FB_HEADLESS       "0" to watch the browser during serve (default headless)
    FB_DEFAULT_CITY   FB city slug used when a search omits one (default vancouver)
"""

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urlparse

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / ".browser-profile"  # the logged-in Chromium session lives here
LOG_PATH = ROOT / "server.log"

DEFAULT_CITY = os.environ.get("FB_DEFAULT_CITY", "vancouver")

# Log to both the console and a file you can `tail -f marketplace-mcp/server.log`.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("finder")

# FB marketplace `sortBy` values, exposed to the agent under friendly names.
SORT_MAP = {
    "best": None,
    "newest": "creation_time_descend",
    "price_low": "price_ascend",
    "price_high": "price_descend",
    "distance": "distance_ascend",
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ─── browser singleton (async Playwright, reused across requests) ─────────────
# One long-lived logged-in context. A lock serializes browser work because a
# single FB session can't sanely run parallel searches anyway.
_pw = None
_ctx = None
_browser_lock = asyncio.Lock()


async def get_context():
    global _pw, _ctx
    if _ctx is not None:
        return _ctx
    from playwright.async_api import async_playwright
    if not PROFILE_DIR.exists():
        raise RuntimeError(
            "No logged-in browser session found. Run `python server.py login` "
            "first and sign into Facebook, then restart the server."
        )
    _pw = await async_playwright().start()
    _ctx = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=os.environ.get("FB_HEADLESS", "1") != "0",
        viewport={"width": 1280, "height": 1600},
        user_agent=UA,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return _ctx


# ─── extraction JS ────────────────────────────────────────────────────────────
# Pull listing cards straight off the search results page — title/price/location
# /photo/url in a single page load, no per-item navigation. Much faster than
# visiting each item; deep detail is available on demand via get_listing_details.
SEARCH_CARDS_JS = r"""
(maxResults) => {
  const out = [];
  const seen = new Set();
  const anchors = Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'));
  for (const a of anchors) {
    const m = (a.getAttribute('href') || '').match(/\/marketplace\/item\/(\d+)/);
    if (!m) continue;
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    // Climb a few levels to the card container that holds price + title + place.
    let node = a;
    for (let i = 0; i < 4 && node.parentElement; i++) node = node.parentElement;
    const text = (node.innerText || '').trim();
    const img = a.querySelector('img') || node.querySelector('img');
    out.push({
      id,
      url: 'https://www.facebook.com/marketplace/item/' + id + '/',
      text,
      photo: img ? img.src : '',
    });
    if (out.length >= maxResults) break;
  }
  return out;
}
"""

# A single item page: og metadata + main-region text + the largest listing photo.
ITEM_EXTRACT_JS = r"""
() => {
  const meta = (p) => {
    const el = document.querySelector(`meta[property="${p}"]`) ||
               document.querySelector(`meta[name="${p}"]`);
    return el ? (el.getAttribute('content') || '') : '';
  };
  const region = document.querySelector('[role="main"]') ||
                 document.querySelector('main') || document.body;
  const text = region ? (region.innerText || '') : '';
  // FB's og:image is often blank (JS-rendered). Real photos carry
  // alt="Photo of <title>"; fall back to the largest fbcdn image.
  const imgs = Array.from(document.querySelectorAll('img[src*="fbcdn.net"]'));
  const listing = imgs.filter((i) => /photo of/i.test(i.alt || ''));
  const pool = listing.length ? listing : imgs;
  pool.sort((a, b) => (b.naturalWidth * b.naturalHeight) - (a.naturalWidth * a.naturalHeight));
  return {
    ogTitle: meta('og:title'),
    ogDesc: meta('og:description'),
    text,
    photo: (meta('og:image') || (pool[0] ? pool[0].src : '')),
  };
}
"""

PRICE_RE = re.compile(r"(?:CA)?\$[\d,]+(?:\.\d{2})?")


def _looks_like_login_wall(url: str, body_text: str) -> bool:
    if "/login" in (urlparse(url).path or ""):
        return True
    low = body_text.lower()
    return ("log in to facebook" in low or "log into facebook" in low) and "marketplace" not in low


def _parse_card(card: dict) -> dict:
    """Turn a card's blob of innerText into best-effort structured fields,
    always keeping the raw text so the agent can recover anything we miss."""
    text = card.get("text", "") or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    price_m = PRICE_RE.search(text)
    price = price_m.group(0) if price_m else None
    # Drop standalone price lines and obvious chrome; FB puts the place last.
    body = [ln for ln in lines if not PRICE_RE.fullmatch(ln)
            and ln.lower() not in ("just listed", "free")]
    title = body[0] if body else None
    location = body[-1] if len(body) > 1 else None
    return {
        "title": title,
        "price": price,
        "location": location,
        "url": card["url"],
        "photo": card.get("photo") or None,
        "raw_text": text[:300],
    }


# ─── MCP server ───────────────────────────────────────────────────────────────
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.server.auth.settings import (  # noqa: E402
    AuthSettings, ClientRegistrationOptions, RevocationOptions,
)

# The SDK's DNS-rebinding protection only trusts localhost by default, so it
# rejects (HTTP 421) requests that arrive via a tunnel hostname. We're exposing
# this through a private tunnel on purpose — and a quick tunnel's hostname
# rotates on every restart — so host-pinning is impractical here. Default to
# off (the secret tunnel URL + optional MCP_AUTH_TOKEN are the real gate); set
# MCP_ALLOWED_HOSTS="host1,host2" to re-enable strict checking.
_allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
if _allowed:
    _security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed,
        allowed_origins=["*"],
    )
else:
    _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_server):
    # Launch the logged-in browser at startup so the first search isn't slowed
    # by a cold Chromium launch — that cold start is what pushes the first call
    # past the client's tool-call timeout.
    try:
        await get_context()
        log.info("browser pre-warmed and ready")
    except Exception as e:
        log.warning("browser pre-warm skipped (%s) — will launch on first search", e)
    yield


# ─── OAuth lockdown (opt-in) ────────────────────────────────────────────────
# Set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + MCP_ALLOWED_EMAILS to require
# OAuth: claude.ai then does the OAuth 2.1 + PKCE + Dynamic Client Registration
# dance against THIS server, and the human gate is a Google sign-in restricted
# to the allowed emails. Without a valid token every /mcp request is 401. Leave
# these unset to keep the legacy behaviour (open, or static MCP_AUTH_TOKEN).
# See oauth.py for the full why.
#
# A static bearer (MCP_AUTH_TOKEN) can't lock down claude.ai — its connector UI
# sends no custom header — and Cloudflare Access is broken for claude.ai web
# (claude-ai-mcp#410). Server-native OAuth is the path that actually works, and
# the SDK emits the RFC 9728 WWW-Authenticate header #410 was missing.
_oauth_provider = None
_auth_kwargs = {}
_public_url = None
if os.environ.get("GOOGLE_CLIENT_ID"):
    from oauth import GoogleOAuthProvider  # noqa: E402

    # Public base URL the metadata documents (and the Google redirect_uri)
    # advertise. Behind the named tunnel this must be the public https host;
    # locally it's 127.0.0.1. Derive a sane default from MCP_ALLOWED_HOSTS (the
    # tunnel host) when MCP_PUBLIC_URL isn't set explicitly.
    _public_url = os.environ.get("MCP_PUBLIC_URL")
    if not _public_url:
        _public_url = (
            f"https://{_allowed[0]}" if _allowed
            else f"http://{os.environ.get('MCP_HOST', '127.0.0.1')}:{os.environ.get('MCP_PORT', '8000')}"
        )
    _oauth_provider = GoogleOAuthProvider(
        google_client_id=os.environ["GOOGLE_CLIENT_ID"],
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        allowed_emails=os.environ.get("MCP_ALLOWED_EMAILS", "").split(","),
        public_url=_public_url,
        store_path=str(ROOT / ".oauth-store.json"),
    )
    _auth_kwargs = dict(
        auth_server_provider=_oauth_provider,
        auth=AuthSettings(
            issuer_url=_public_url,
            resource_server_url=f"{_public_url.rstrip('/')}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[],          # any valid token is accepted (allowlisted user)
        ),
    )

# Transport hardening for running behind a flaky tunnel:
#   stateless_http=True  — no long-lived MCP session / server->client SSE stream.
#       Each tool call is one independent request, so a tunnel reconnect between
#       calls is harmless. This is the key fix: the previous hangs were claude.ai
#       waiting on a persistent stream that the tunnel's QUIC reconnects killed.
#   json_response=True   — return each result as a single JSON POST response
#       instead of an SSE stream, so a reconnect can't truncate a result mid-flight.
mcp = FastMCP("marketplace-finder", transport_security=_security,
              lifespan=_lifespan, json_response=True, stateless_http=True,
              **_auth_kwargs)


if _oauth_provider is not None:
    @mcp.custom_route("/oauth/google/callback", methods=["GET"])
    async def _oauth_google_callback(request):
        q = request.query_params
        return await _oauth_provider.google_callback(
            code=q.get("code", ""), state=q.get("state", ""), error=q.get("error"),
        )


@mcp.tool()
async def search_facebook_marketplace(
    query: str,
    city: str = "",
    min_price: int | None = None,
    max_price: int | None = None,
    radius_km: int | None = None,
    days_listed: int | None = None,
    sort: str = "best",
    max_results: int = 12,
    offset: int = 0,
) -> dict:
    """Search Facebook Marketplace for an item and return matching listings.

    Runs in the user's own logged-in browser on their Mac. Ask the user for any
    missing details (especially budget and city) before calling this.

    Args:
        query: What to look for, e.g. "Herman Miller Aeron chair", "Yeti cooler".
        city: Facebook city slug, e.g. "vancouver", "seattle", "nyc", "la".
              Defaults to the server's FB_DEFAULT_CITY.
        min_price: Minimum price filter (currency of the marketplace).
        max_price: Maximum price filter.
        radius_km: Search radius in km around the city.
        days_listed: Only items listed within the last N days (e.g. 1, 7).
        sort: One of "best", "newest", "price_low", "price_high", "distance".
        max_results: Cap on listings returned per call (1–50).
        offset: Skip this many results before this page. For more results after
            a first call, call again with the SAME arguments and offset set to
            the previous response's next_offset.

    Returns:
        {"query", "search_url", "offset", "count", "has_more", "next_offset",
         "hint", "listings": [
            {"title", "price", "location", "url", "photo", "raw_text"}, ...]}
        When has_more is true there are more results — call again with
        offset=next_offset to fetch the next page. The hint says so in words.
    """
    city = (city or DEFAULT_CITY).strip().lower().replace(" ", "")
    max_results = max(1, min(int(max_results), 50))
    offset = max(0, int(offset))
    gather = offset + max_results + 1  # +1 to detect whether a next page exists

    params = [f"query={quote_plus(query)}", "exact=false"]
    if min_price is not None:
        params.append(f"minPrice={int(min_price)}")
    if max_price is not None:
        params.append(f"maxPrice={int(max_price)}")
    if radius_km is not None:
        params.append(f"radius={int(radius_km)}")
    if days_listed is not None:
        params.append(f"daysSinceListed={int(days_listed)}")
    sort_val = SORT_MAP.get(sort.lower())
    if sort_val:
        params.append(f"sortBy={sort_val}")
    search_url = f"https://www.facebook.com/marketplace/{city}/search/?" + "&".join(params)

    t0 = time.monotonic()
    log.info("search: query=%r city=%s offset=%d max=%d", query, city, offset, max_results)
    try:
        async with _browser_lock:
            ctx = await get_context()
            page = await ctx.new_page()
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
                body_text = await page.inner_text("body")
                if _looks_like_login_wall(page.url, body_text):
                    raise RuntimeError(
                        "Facebook is showing a login wall — the saved session has "
                        "expired. Run `python server.py login` again, then retry."
                    )
                # Facebook search is an infinite-scroll feed. Scroll until we've
                # loaded enough cards to cover offset+max_results (+1, to tell
                # whether a next page exists), or the feed stops growing.
                cards = await page.evaluate(SEARCH_CARDS_JS, gather)
                stagnant = 0
                for _ in range(min(12, 2 + gather // 6)):
                    if len(cards) >= gather:
                        break
                    prev = len(cards)
                    await page.mouse.wheel(0, 6000)
                    await page.wait_for_timeout(900)
                    cards = await page.evaluate(SEARCH_CARDS_JS, gather)
                    if len(cards) <= prev:
                        stagnant += 1
                        if stagnant >= 2:  # feed exhausted — stop scrolling
                            break
                    else:
                        stagnant = 0
            finally:
                await page.close()
    except Exception as e:
        log.exception("search failed after %.1fs: %s", time.monotonic() - t0, e)
        raise

    parsed = [_parse_card(c) for c in cards]
    listings = parsed[offset:offset + max_results]
    has_more = len(parsed) > offset + max_results
    next_offset = offset + len(listings)
    if not listings:
        hint = ("No more results — you've reached the end of this search."
                if offset else "No matching listings found.")
    elif has_more:
        hint = (f"Showing results {offset + 1}-{next_offset}. More are available — "
                f"call this tool again with the same arguments and offset={next_offset} "
                "to get the next page.")
    else:
        hint = (f"Showing results {offset + 1}-{next_offset}. "
                "These are all the results Facebook returned for this search.")
    log.info("search: %d listings (offset %d, has_more=%s) in %.1fs",
             len(listings), offset, has_more, time.monotonic() - t0)
    return {
        "query": query,
        "search_url": search_url,
        "offset": offset,
        "count": len(listings),
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "hint": hint,
        "listings": listings,
    }


@mcp.tool()
async def get_listing_details(url: str) -> dict:
    """Fetch the full details of one Facebook Marketplace listing.

    Use after search_facebook_marketplace when the user wants the complete
    description, condition, or seller notes for a specific item.

    Args:
        url: A facebook.com/marketplace/item/... URL.

    Returns:
        {"url", "title", "description", "price", "photo", "full_text"}
    """
    if "facebook.com/marketplace/item/" not in url:
        raise ValueError("url must be a facebook.com/marketplace/item/... link")

    async with _browser_lock:
        ctx = await get_context()
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            # Expand any "See more" so the description isn't truncated.
            for sel in ["text=/see more/i", "div[role='button']:has-text('See more')"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=1500)
                        await page.wait_for_timeout(400)
                        break
                except Exception:
                    pass
            data = await page.evaluate(ITEM_EXTRACT_JS)
            page_title = (await page.title() or "")
        finally:
            await page.close()

    full = re.sub(r"\n{3,}", "\n\n", (data.get("text") or "")).strip()[:6000]
    price_m = PRICE_RE.search(full)
    title = (data.get("ogTitle") or page_title).replace(" | Facebook", "").strip()
    return {
        "url": url,
        "title": title or None,
        "description": (data.get("ogDesc") or "").strip() or None,
        "price": price_m.group(0) if price_m else None,
        "photo": data.get("photo") or None,
        "full_text": full,
    }


# ─── login (one-time, interactive, headed) ────────────────────────────────────
def cmd_login():
    from playwright.sync_api import sync_playwright
    print("Opening a browser. Log into Facebook, then close the window.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 1600},
            user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()
    print("Session saved to", PROFILE_DIR)


# ─── serve ────────────────────────────────────────────────────────────────────
class _TokenAuth:
    """Tiny ASGI gate: require `Authorization: Bearer <token>` when one is set."""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if auth != f"Bearer {self.token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def cmd_serve():
    import uvicorn
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))
    token = os.environ.get("MCP_AUTH_TOKEN")

    app = mcp.streamable_http_app()  # MCP endpoint mounted at /mcp
    if _oauth_provider is not None:
        # OAuth is enforced by the SDK's bearer middleware on /mcp; don't also
        # wrap with _TokenAuth (it would 401 the /authorize, /token, /register
        # endpoints and break the flow). OAuth takes precedence over any token.
        print(f"Auth: OAuth 2.1 via Google sign-in (works with claude.ai)")
        print(f"  allowed emails: {', '.join(sorted(_oauth_provider.allowed_emails))}")
        print(f"  Google redirect URI: {_oauth_provider.redirect_uri}")
        if token:
            print("  note: MCP_AUTH_TOKEN is ignored while OAuth is configured")
    elif token:
        app = _TokenAuth(app, token)
        print("Auth: requiring Bearer token from MCP_AUTH_TOKEN")
    else:
        print("Auth: OPEN (no GOOGLE_CLIENT_ID / MCP_AUTH_TOKEN set) — keep your tunnel URL private")

    print(f"Marketplace Finder MCP on http://{host}:{port}/mcp")
    print("Expose it, e.g.:  cloudflared tunnel --url http://localhost:%d" % port)
    uvicorn.run(app, host=host, port=port)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "login":
        cmd_login()
    elif cmd == "serve":
        cmd_serve()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
