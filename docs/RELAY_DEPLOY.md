# Relay deploy runbook (mcp.wynnset.com)

Turnkey steps to deploy the relay Worker and cut `mcp.wynnset.com` over from the old
per-friend cloudflared tunnel to the single shared relay. Steps marked **[you]** need
your Cloudflare/Google auth or an explicit decision; the rest can be run for you.

> Status when this was written: Steps 2–4 done + verified; `finder provision` done.
> `wrangler` is **not** logged in on this Mac yet; `mcp.wynnset.com` still resolves to
> the old cloudflared tunnel. Nothing below has been run.

## 0. Prereqs

```bash
cd relay && npm install        # installs wrangler + @cloudflare/workers-oauth-provider
```

- **[you] Authorize wrangler to Cloudflare** (interactive, opens a browser):
  ```bash
  npx wrangler login
  ```
  Or export a scoped `CLOUDFLARE_API_TOKEN` (perms: *Workers Scripts:Edit*,
  *Workers KV Storage:Edit*, *Workers Routes:Edit*, and *DNS:Edit* on the
  wynnset.com zone) so the deploy can run non-interactively.

## 1. KV namespaces

```bash
npx wrangler kv namespace create OAUTH_KV
npx wrangler kv namespace create RELAY_KV
```

Paste the two printed `id` values into `relay/wrangler.jsonc` → `kv_namespaces`
(replacing `REPLACE_WITH_OAUTH_KV_ID` / `REPLACE_WITH_RELAY_KV_ID`).

## 2. Secrets

Pick a strong shared signing secret **once** and use the SAME value in two places:
the Worker (verifies device tokens) and `.finder.env` (mints them in `finder provision`).

```bash
# generate and KEEP this value (do not commit it):
openssl rand -base64 48

cd relay
npx wrangler secret put RELAY_JWT_SECRET      # paste the value above
npx wrangler secret put GOOGLE_CLIENT_ID      # from .finder.env
npx wrangler secret put GOOGLE_CLIENT_SECRET  # from .finder.env
npx wrangler secret put MCP_ALLOWED_EMAILS    # comma-separated allowlist (e.g. aidin@wynnset.com)
```

`PUBLIC_URL` is already set as a non-secret var in `wrangler.jsonc`
(`https://mcp.wynnset.com`).

## 3. Google Cloud OAuth client  **[you]**

In the existing OAuth 2.0 **Web application** client
(https://console.cloud.google.com/apis/credentials), add this **Authorized redirect
URI** exactly (the relay's callback):

```
https://mcp.wynnset.com/oauth/google/callback
```

(The old per-host callback can be removed once cutover is confirmed working.)

## 4. DNS cutover  **[you — decision]**

`mcp.wynnset.com` currently points at the old cloudflared tunnel. The Worker takes
the same hostname, so before deploy **remove the existing DNS record** for
`mcp.wynnset.com` (Cloudflare dashboard → DNS → delete the CNAME to
`<uuid>.cfargotunnel.com`), and stop the old tunnel:

```bash
./finder stop            # stops the old server + tunnel launchd services
```

`custom_domain: true` in `wrangler.jsonc` will then recreate the hostname pointing at
the Worker on deploy. (If you'd rather keep the old tunnel running on a different host
during testing, deploy the Worker to a temporary `relay.wynnset.com` first.)

## 5. Deploy

```bash
cd relay && npx wrangler deploy
```

## 6. Smoke test

```bash
# metadata + the RFC 9728 challenge claude.ai needs:
curl -s https://mcp.wynnset.com/.well-known/oauth-protected-resource | jq .
curl -s -i -X POST https://mcp.wynnset.com/mcp | grep -i www-authenticate

# then, end to end:
#   - provision yourself:  RELAY_JWT_SECRET + RELAY_URL in .finder.env, then
#       ./finder provision aidin@wynnset.com
#   - run the agent on this Mac (the finder cutover step wires this as a LaunchAgent)
#   - in claude.ai add the connector  https://mcp.wynnset.com/mcp  and sign in with Google
```

## After deploy — remaining (separate, gated) work

- **finder cutover:** remove cloudflared (`ensure_tunnel`, the tunnel LaunchAgent,
  `MCP_ALLOWED_HOSTS`, `--protocol http2`); switch the server LaunchAgent to run
  `relay/relay_client.py` (env `RELAY_URL` + `RELAY_DEVICE_TOKEN`, + `caffeinate`);
  teach `save_config` to persist `RELAY_URL` + `RELAY_JWT_SECRET` (today it would
  overwrite them). Then add those two keys to `.finder.env`.
- **notarized installer** (`.app`/`.pkg`) for non-technical friends.
- **CLAUDE.md** architecture cutover (demote cloudflared lessons #1/#3 to historical).
