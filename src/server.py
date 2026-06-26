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

Commands (run from the repo root; this file lives in src/):
    python src/server.py login     # one-time: open a browser, log into Facebook
    python src/server.py serve      # run the MCP server (default)

Setup (once):
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    python -m playwright install chromium
    python src/server.py login
    python src/server.py serve
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
import json
import logging
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus, urlparse

# server.py lives in src/; runtime data (browser profile, oauth store, logs,
# caches) stays at the repo root one level up — so ROOT resolves to the parent.
ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / ".browser-profile"  # the logged-in Chromium session lives here
LOG_PATH = ROOT / "server.log"
GEOCODE_CACHE = ROOT / ".geocode-cache.json"  # place-name -> [lat,lng] memo (gitignored)

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
            "No logged-in browser session found. Run `python src/server.py login` "
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
# Card text for rentals reads like "CA$4,000 · 3 Beds 2 Baths Apartment · City".
BEDS_RE = re.compile(r"(\d+)\s*Bed", re.I)
BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.I)
AREA_RE = re.compile(r"([\d,]{3,})\s*(?:sq\.?\s*ft|sqft|square\s*fe?e?t)", re.I)
PROPTYPE_RE = re.compile(
    r"\b(Apartment|Condo|Townhouse|House|Duplex|Studio|Basement|Suite|Room|Laneway)\b", re.I)

# FB's radius dropdown only offers these steps (km). We snap a requested radius
# UP to the nearest available one when driving the picker.
RADIUS_STEPS = [1, 2, 5, 10, 20, 40, 60, 80, 100, 250, 500]

# ─── geocoding + distance (server-side radius filter) ─────────────────────────
# FB ignores `radius`/`latitude`/`longitude` in the search URL — it only honours
# the account's location-picker state. So we *also* filter results by distance
# ourselves: geocode each card's city name and drop ones beyond the radius. This
# is the deterministic guarantee; the picker (below) is best-effort on top.
# Coarse centroids for Metro Vancouver (and a few anchors) make the common case
# work offline with no network call. Anything else is resolved via Nominatim and
# cached to disk. Keyed by the lowercased city token (first comma-part).
_GAZETTEER = {
    "vancouver": (49.2606, -123.1140),
    "downtown vancouver": (49.2820, -123.1171),
    "west vancouver": (49.3286, -123.1602),
    "north vancouver": (49.3200, -123.0724),
    "north vancouver district": (49.3637, -123.0150),
    "burnaby": (49.2488, -122.9805),
    "new westminster": (49.2057, -122.9110),
    "richmond": (49.1666, -123.1336),
    "coquitlam": (49.2838, -122.7932),
    "port coquitlam": (49.2624, -122.7811),
    "port moody": (49.2849, -122.8678),
    "surrey": (49.1913, -122.8490),
    "delta": (49.0847, -123.0587),
    "ladner": (49.0890, -123.0827),
    "tsawwassen": (49.0119, -123.0840),
    "langley": (49.1044, -122.6604),
    "white rock": (49.0253, -122.8029),
    "maple ridge": (49.2193, -122.5984),
    "pitt meadows": (49.2214, -122.6892),
    "abbotsford": (49.0504, -122.3045),
    "mission": (49.1337, -122.3115),
    "chilliwack": (49.1579, -121.9514),
    "squamish": (49.7016, -123.1558),
}

_geocode_mem = None  # the disk cache, loaded once

# Nominatim's usage policy: max 1 req/sec, a real User-Agent, and a contact so
# they can reach us before blocking. We serialize calls and space them ≥1s apart.
# Override the contact via GEOCODE_CONTACT_EMAIL.
NOMINATIM_CONTACT = os.environ.get("GEOCODE_CONTACT_EMAIL", "aidin@wynnset.com")
NOMINATIM_UA = f"marketplace-finder-mcp/1.0 ({NOMINATIM_CONTACT})"
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0  # monotonic time of the last outbound request


def _haversine_km(a: tuple, b: tuple) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# Larger offline fallback: ~1500 North American city centroids (top 1000 US +
# top 500 CA by population). Keyed the same way as _norm_place produces, so a
# card's "City, ST" resolves offline. See gazetteer.py / ../scripts/build_gaz.py.
try:
    from gazetteer import CITIES as _NA_CITIES
except Exception as e:  # missing/corrupt file → fall back to Nominatim only
    log.warning("gazetteer not loaded (%s) — relying on curated table + Nominatim", e)
    _NA_CITIES = {}


def _norm_place(name: str) -> str:
    """City token from a "City, Region" string, lowercased, accents stripped,
    and 'St.'/'St ' → 'Saint' — matching how the gazetteers are keyed."""
    s = name.split(",")[0]
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).strip().lower()
    return re.sub(r"^st\.?\s+", "saint ", s)


async def _geocode(name: str):
    """Best-effort (lat, lng) for a place string: gazetteer → disk cache →
    Nominatim. Returns None if all fail (the caller then keeps the listing
    rather than dropping something it can't place)."""
    global _geocode_mem
    if not name:
        return None
    head = _norm_place(name)
    if head in _GAZETTEER:      # curated Metro-Vancouver table (most precise)
        return _GAZETTEER[head]
    if head in _NA_CITIES:      # broad NA fallback (city centroids)
        return _NA_CITIES[head]
    if _geocode_mem is None:
        try:
            _geocode_mem = json.loads(GEOCODE_CACHE.read_text())
        except Exception:
            _geocode_mem = {}
    key = name.strip().lower()
    if key in _geocode_mem:
        v = _geocode_mem[key]
        return tuple(v) if v else None
    coord = None
    try:
        import httpx
        # Honour Nominatim's ≤1 req/sec policy: one in flight at a time, spaced.
        global _nominatim_last
        async with _nominatim_lock:
            wait = 1.0 - (time.monotonic() - _nominatim_last)
            if wait > 0:
                await asyncio.sleep(wait)
            async with httpx.AsyncClient(timeout=8) as c:
                resp = await c.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": name, "format": "json", "limit": 1,
                            "email": NOMINATIM_CONTACT},
                    headers={"User-Agent": NOMINATIM_UA},
                )
            _nominatim_last = time.monotonic()
            arr = resp.json()
            if arr:
                coord = (float(arr[0]["lat"]), float(arr[0]["lon"]))
    except Exception as e:
        log.warning("geocode failed for %r: %s", name, e)
    _geocode_mem[key] = list(coord) if coord else None
    try:
        GEOCODE_CACHE.write_text(json.dumps(_geocode_mem))
    except Exception:
        pass
    return coord


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
    # Rentals encode beds/baths/type/area right in the card text — surface them
    # as structured fields so the agent can filter (it couldn't before).
    beds_m = BEDS_RE.search(text)
    baths_m = BATHS_RE.search(text)
    area_m = AREA_RE.search(text)
    type_m = PROPTYPE_RE.search(text)
    return {
        "title": title,
        "price": price,
        "location": location,
        "bedrooms": int(beds_m.group(1)) if beds_m else None,
        "bathrooms": float(baths_m.group(1)) if baths_m else None,
        "property_type": type_m.group(1).title() if type_m else None,
        "area_sqft": int(area_m.group(1).replace(",", "")) if area_m else None,
        "url": card["url"],
        "photo": card.get("photo") or None,
        "raw_text": text[:300],
    }


async def _set_search_location(page, location: str, radius_km: int) -> str | None:
    """Drive FB's "Change location" dialog to set the search centre + radius.

    FB ignores radius in the URL, so this UI state is the only thing it actually
    honours. Best-effort: any failure is logged and swallowed — the server-side
    distance filter is the real guarantee. Returns the radius label it set (e.g.
    "5 kilometers") or None if it couldn't.
    """
    step = next((s for s in RADIUS_STEPS if s >= radius_km), RADIUS_STEPS[-1])
    try:
        pill = page.get_by_text(re.compile(r"Within\s+\d+\s*km")).first
        if not await pill.count():
            log.warning("location pill not found — skipping picker")
            return None
        await pill.click(timeout=4000)
        await page.wait_for_timeout(1500)
        dialog = page.locator('[role="dialog"]').last
        if location:
            loc_in = dialog.get_by_role("combobox", name="Location").first
            await loc_in.click(timeout=3000)
            try:
                await loc_in.fill(location, timeout=3000)
            except Exception:
                await page.keyboard.type(location, delay=20)
            await page.wait_for_timeout(1600)
            opt = page.get_by_role("option").first  # first autocomplete match
            if await opt.count():
                await opt.click(timeout=3000)
                await page.wait_for_timeout(800)
        radcb = dialog.locator('[role="combobox"]').filter(has_text="kilomet").first
        if await radcb.count():
            await radcb.click(timeout=3000)
            await page.wait_for_timeout(800)
            label = f"{step} kilometer" + ("" if step == 1 else "s")
            ropt = page.get_by_role("option", name=label, exact=True).first
            if await ropt.count():
                await ropt.click(timeout=3000)
                await page.wait_for_timeout(600)
        await dialog.get_by_role("button", name="Apply").first.click(timeout=3000)
        await page.wait_for_timeout(3500)
        log.info("picker set: place=%r radius<=%dkm", location, step)
        return f"{step} kilometers"
    except Exception as e:
        log.warning("could not set location/radius via picker (%s) — "
                    "relying on the distance filter", e)
        return None


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
    near: str = "",
    min_bedrooms: int | None = None,
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
        radius_km: Search radius in km. NOTE: Facebook ignores radius in the URL,
            so this is enforced two ways here — (1) the server drives FB's
            location picker to set the radius, and (2) results are filtered
            server-side by distance from the centre (`near`, else `city`).
            Distance filtering is CITY-LEVEL: it reliably excludes other cities
            (e.g. Surrey/Abbotsford when centred on Vancouver) but cannot do true
            sub-city precision (it can't tell 2km from 8km *within* one city).
        near: A precise place to centre the radius on — a neighbourhood, address,
            or landmark, e.g. "Science World, Vancouver" or "Mount Pleasant,
            Vancouver". Improves both the picker and the distance filter. When
            omitted, the radius is centred on `city`.
        min_bedrooms: Keep only listings with at least this many bedrooms.
            Listings whose bedroom count can't be read are kept (not dropped).
        days_listed: Only items listed within the last N days (e.g. 1, 7).
        sort: One of "best", "newest", "price_low", "price_high", "distance".
        max_results: Cap on listings returned per call (1–50).
        offset: Skip this many results before this page. For more results after
            a first call, call again with the SAME arguments and offset set to
            the previous response's next_offset.

    Returns:
        {"query", "search_url", "offset", "count", "has_more", "next_offset",
         "hint", "listings": [
            {"title", "price", "location", "bedrooms", "bathrooms",
             "property_type", "area_sqft", "url", "photo", "raw_text"}, ...]}
        bedrooms/bathrooms/property_type/area_sqft are parsed from the card and
        may be null. area_sqft is rarely on the card — use get_listing_details
        for square footage. When has_more is true call again with
        offset=next_offset for the next page; the hint says so in words.
    """
    city = (city or DEFAULT_CITY).strip().lower().replace(" ", "")
    max_results = max(1, min(int(max_results), 50))
    offset = max(0, int(offset))
    filtering = radius_km is not None or min_bedrooms is not None
    # Pull a deeper pool when filtering server-side, so a page still fills after
    # culling out-of-radius / too-few-bedroom cards.
    gather = offset + max_results + 1  # +1 to detect whether a next page exists
    if filtering:
        gather = min(120, (offset + max_results) * 4 + 1)

    # Note: FB ignores `radius`/`latitude`/`longitude` in the URL, so we don't
    # bother adding them — radius is handled by the picker + distance filter.
    params = [f"query={quote_plus(query)}", "exact=false"]
    if min_price is not None:
        params.append(f"minPrice={int(min_price)}")
    if max_price is not None:
        params.append(f"maxPrice={int(max_price)}")
    if days_listed is not None:
        params.append(f"daysSinceListed={int(days_listed)}")
    sort_val = SORT_MAP.get(sort.lower())
    if sort_val:
        params.append(f"sortBy={sort_val}")
    search_url = f"https://www.facebook.com/marketplace/{city}/search/?" + "&".join(params)

    t0 = time.monotonic()
    log.info("search: query=%r city=%s near=%r radius=%s min_bed=%s offset=%d max=%d",
             query, city, near, radius_km, min_bedrooms, offset, max_results)
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
                        "expired. Run `python src/server.py login` again, then retry."
                    )
                # Make FB itself honour the radius (it ignores the URL param):
                # drive its location picker. Best-effort; the distance filter
                # below is the real guarantee. `near` re-centres precisely.
                if radius_km is not None:
                    await _set_search_location(page, near, radius_km)
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

    # ── server-side filters (applied before pagination so pages stay coherent) ─
    notes = []
    if min_bedrooms is not None:
        before = len(parsed)
        parsed = [p for p in parsed
                  if p["bedrooms"] is None or p["bedrooms"] >= min_bedrooms]
        if before - len(parsed):
            notes.append(f"{before - len(parsed)} dropped below {min_bedrooms} bedrooms")
    if radius_km is not None:
        center = await _geocode(near or city)
        if center:
            # City-centroid granularity is coarse; allow slack so listings in the
            # centre city itself aren't culled, while other cities still drop.
            slack = radius_km * 1.25 + 1.5
            kept, dropped = [], 0
            for p in parsed:
                c = await _geocode(p["location"]) if p["location"] else None
                if c and _haversine_km(center, c) > slack:
                    dropped += 1
                    continue
                kept.append(p)
            parsed = kept
            if dropped:
                notes.append(f"{dropped} dropped beyond ~{radius_km}km of "
                             f"{near or city}")
        else:
            notes.append(f"couldn't geocode centre {near or city!r}; "
                         "distance filter skipped (picker still applied)")

    listings = parsed[offset:offset + max_results]
    has_more = len(parsed) > offset + max_results
    next_offset = offset + len(listings)
    filt = (" Filters: " + "; ".join(notes) + ".") if notes else ""
    if not listings:
        hint = ("No more results — you've reached the end of this search."
                if offset else "No matching listings found.") + filt
    elif has_more:
        hint = (f"Showing results {offset + 1}-{next_offset}. More are available — "
                f"call this tool again with the same arguments and offset={next_offset} "
                "to get the next page." + filt)
    else:
        hint = (f"Showing results {offset + 1}-{next_offset}. "
                "These are all the results Facebook returned for this search." + filt)
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
        {"url", "title", "description", "price", "bedrooms", "bathrooms",
         "property_type", "area_sqft", "photo", "full_text"}
        Square footage (area_sqft) usually only appears here, not on search cards.
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
    # Parse the same structured fields as the search cards, plus area_sqft, which
    # is usually only present on the full listing. Search the title+desc first
    # (concise) then the full body as a fallback.
    head = f"{title}\n{data.get('ogDesc') or ''}\n{full}"
    beds_m = BEDS_RE.search(head)
    baths_m = BATHS_RE.search(head)
    area_m = AREA_RE.search(head)
    type_m = PROPTYPE_RE.search(head)
    return {
        "url": url,
        "title": title or None,
        "description": (data.get("ogDesc") or "").strip() or None,
        "price": price_m.group(0) if price_m else None,
        "bedrooms": int(beds_m.group(1)) if beds_m else None,
        "bathrooms": float(baths_m.group(1)) if baths_m else None,
        "property_type": type_m.group(1).title() if type_m else None,
        "area_sqft": int(area_m.group(1).replace(",", "")) if area_m else None,
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
