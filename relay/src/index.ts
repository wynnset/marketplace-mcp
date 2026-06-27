/**
 * marketplace-relay — Cloudflare Worker + Durable Object  (STEP 2: OAuth gate)
 *
 * The single public endpoint for every friend's Mac. One URL, no per-friend
 * subdomain: requests are routed to the right Mac by *identity*, not by hostname.
 *
 *   claude.ai ──POST /mcp──▶ Worker ─▶ DO(identity) ─┐
 *   Alice Mac ──WSS /agent─▶ Worker ─▶ DO(identity) ◀┘ (holds the live socket)
 *
 * AUTH (Step 2): the claude.ai-facing OAuth authorization server is provided by
 * `@cloudflare/workers-oauth-provider` — it owns DCR, PKCE, /token, /register,
 * /revoke, the metadata docs, and the RFC 9728 `WWW-Authenticate` header claude.ai
 * requires. We supply only the human gate (Google sign-in + email allowlist, in
 * google_gate.ts) and the API handler that routes an authenticated /mcp request to
 * `DO(sub)` using the library-verified `ctx.props.sub`. The access token is the
 * library's own opaque, KV-backed token — NOT a JWT (see docs/RELAY_MIGRATION.md).
 *
 * The /agent leg still uses the Step-1 `X-Relay-Identity` stub; Step 3 swaps it for
 * a verified device-JWT (signed with RELAY_JWT_SECRET).
 */

import OAuthProvider, {
  type AuthRequest,
  type OAuthHelpers,
} from "@cloudflare/workers-oauth-provider";
import {
  GOOGLE_CALLBACK_PATH,
  allowedEmails,
  buildGoogleAuthUrl,
  denyPage,
  expiredPage,
  fetchGoogleEmail,
} from "./google_gate";

/** Encrypted into the grant by the library; handed back to the API handler as ctx.props. */
interface Props {
  sub: string; // verified, lowercased email — the routing key
}

export interface Env {
  RELAY: DurableObjectNamespace;
  OAUTH_KV: KVNamespace; // the library's grant/client/token store
  RELAY_KV: KVNamespace; // our own state (in-flight authorize state; Step-3 denylist)
  OAUTH_PROVIDER: OAuthHelpers; // helper methods injected by the library
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  MCP_ALLOWED_EMAILS: string;
  PUBLIC_URL: string;
  RELAY_JWT_SECRET: string; // HS256 secret for device tokens (Mac → /agent)
  GOOGLE_AUTH_URL?: string;
  GOOGLE_TOKEN_URL?: string;
  GOOGLE_USERINFO_URL?: string;
}

const IDENTITY_HEADER = "x-relay-identity";
const AGENT_TIMEOUT_MS = 30_000;
const ACCESS_TOKEN_TTL = 60 * 60 * 24; // 1 day, matching the old oauth.py
const AUTHZ_STATE_PREFIX = "authz_state:";
const AUTHZ_STATE_TTL = 300; // 5 min — an in-flight authorize request is short-lived
const DEVICE_AUD = "relay-agent"; // device tokens carry this aud (≠ access tokens)

// Headers we never forward down to the Mac: routing/authority framing and the
// bearer token (the Mac app runs open behind the relay; it has no use for it).
const STRIP_TO_AGENT = new Set([IDENTITY_HEADER, "authorization"]);

function rpcError(message: string, code: number, httpStatus: number): Response {
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", error: { code, message }, id: null }),
    { status: httpStatus, headers: { "content-type": "application/json" } },
  );
}

// ── /mcp: authenticated MCP traffic. The library has already validated the bearer
//    token and decrypted the grant into ctx.props; route to that identity's DO. ────
const apiHandler = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const props = (ctx as ExecutionContext & { props?: Props }).props;
    const sub = props?.sub?.toLowerCase();
    if (!sub) {
      // Should never happen — the library only invokes us for valid tokens.
      return rpcError("token has no identity", -32003, 401);
    }
    const id = env.RELAY.idFromName(sub);
    return env.RELAY.get(id).fetch(request);
  },
};

// ── everything that is not /mcp or a library-owned OAuth endpoint ─────────────────
const defaultHandler = {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/healthz") {
      return new Response("ok\n", { headers: { "content-type": "text/plain" } });
    }

    // Mac agent registers here. Identity comes ONLY from a verified device-JWT
    // (HS256, signed by finder with RELAY_JWT_SECRET) — never a client header
    // (security invariant #1). The token is a secret carried in Authorization.
    if (url.pathname === "/agent") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("expected websocket upgrade", { status: 426 });
      }
      const auth = request.headers.get("Authorization") || "";
      const token = /^bearer /i.test(auth) ? auth.slice(7).trim() : "";
      const claims = token ? await verifyDeviceJWT(token, env.RELAY_JWT_SECRET) : null;
      if (!claims) {
        // Refuse the upgrade. An opaque access token can't pass here (not a JWT),
        // keeping access ≠ device tokens separated by construction (invariant #3).
        return new Response("invalid or missing device token", { status: 401 });
      }
      if (await isRevoked(env, claims)) {
        return new Response("device token revoked", { status: 401 });
      }
      const id = env.RELAY.idFromName(claims.sub);
      return env.RELAY.get(id).fetch(request);
    }

    // /authorize → bounce the browser to Google sign-in (the library advertises this
    // path in its metadata but delegates the actual login to us).
    if (url.pathname === "/authorize") {
      const oauthReq = await env.OAUTH_PROVIDER.parseAuthRequest(request);
      // Persist the in-flight request keyed by an unguessable state so the Google
      // round-trip can resume it (mirrors oauth.py's _pending dict).
      const state = crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().replace(/-/g, "");
      await env.RELAY_KV.put(AUTHZ_STATE_PREFIX + state, JSON.stringify(oauthReq), {
        expirationTtl: AUTHZ_STATE_TTL,
      });
      return Response.redirect(buildGoogleAuthUrl(env, state), 302);
    }

    // Google redirect lands here: verify the account, then complete the MCP authorize.
    if (url.pathname === GOOGLE_CALLBACK_PATH) {
      const state = url.searchParams.get("state") || "";
      const code = url.searchParams.get("code") || "";
      const error = url.searchParams.get("error");

      const stored = state ? await env.RELAY_KV.get(AUTHZ_STATE_PREFIX + state) : null;
      if (!stored) return expiredPage();
      await env.RELAY_KV.delete(AUTHZ_STATE_PREFIX + state);
      const oauthReq = JSON.parse(stored) as AuthRequest;

      if (error || !code) {
        return denyPage(`Google sign-in was cancelled (${error || "no code"}).`);
      }

      let email = "";
      let verified = false;
      try {
        ({ email, verified } = await fetchGoogleEmail(env, code));
      } catch {
        return denyPage("Could not verify your Google account. Try again.");
      }

      if (!verified || !allowedEmails(env).has(email.toLowerCase())) {
        return denyPage(`${email || "This account"} is not authorized for this server.`);
      }

      const sub = email.toLowerCase();
      const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
        request: oauthReq,
        userId: sub,
        metadata: { email: sub },
        scope: oauthReq.scope ?? [],
        props: { sub } satisfies Props,
      });
      return Response.redirect(redirectTo, 302);
    }

    return new Response("not found", { status: 404 });
  },
};

export default new OAuthProvider({
  apiRoute: "/mcp",
  // The library's handler types are nominal; our plain ExportedHandlers satisfy the
  // runtime contract (fetch(request, env, ctx) with ctx.props on the API side).
  apiHandler: apiHandler as never,
  defaultHandler: defaultHandler as never,
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",
  scopesSupported: ["openid", "email", "profile", "mcp"],
  accessTokenTTL: ACCESS_TOKEN_TTL,
});

// ── Durable Object: rendezvous between claude.ai's /mcp request and the Mac's WS ──
interface ResEnvelope {
  t: "res";
  cid: string;
  status: number;
  headers: [string, string][];
  body_b64: string;
}

export class RelayDO {
  private agent: WebSocket | null = null;
  private pending = new Map<string, (env: ResEnvelope) => void>();
  private seq = 0;

  constructor(_state: DurableObjectState, _env: Env) {}

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/agent") {
      const pair = new WebSocketPair();
      const client = pair[0];
      const server = pair[1];
      server.accept();
      this.attachAgent(server);
      return new Response(null, { status: 101, webSocket: client });
    }

    if (url.pathname === "/mcp") {
      if (!this.agent) {
        return rpcError("agent offline (the Mac is not connected)", -32001, 503);
      }
      const cid = `${this.seq++}`;
      const bodyBuf = await req.arrayBuffer();
      const envelope = {
        t: "req",
        cid,
        method: req.method,
        path: "/mcp",
        headers: [...req.headers].filter(([k]) => !STRIP_TO_AGENT.has(k.toLowerCase())),
        body_b64: b64encode(bodyBuf),
      };

      const result = new Promise<ResEnvelope>((resolve, reject) => {
        const timer = setTimeout(() => {
          this.pending.delete(cid);
          reject(new Error("timed out waiting for the Mac"));
        }, AGENT_TIMEOUT_MS);
        this.pending.set(cid, (env) => {
          clearTimeout(timer);
          resolve(env);
        });
      });

      try {
        this.agent.send(JSON.stringify(envelope));
        const res = await result;
        return new Response(b64decode(res.body_b64), {
          status: res.status,
          headers: res.headers,
        });
      } catch (e) {
        return rpcError(String((e as Error)?.message ?? e), -32002, 504);
      }
    }

    return new Response("not found", { status: 404 });
  }

  private attachAgent(ws: WebSocket): void {
    // One active agent per identity: a fresh registration replaces the old socket
    // (a stale/duplicate Mac can't shadow the live one).
    if (this.agent) {
      try { this.agent.close(1012, "replaced by a newer connection"); } catch {}
    }
    this.agent = ws;

    ws.addEventListener("message", (ev: MessageEvent) => {
      if (typeof ev.data !== "string") return;
      let env: ResEnvelope;
      try { env = JSON.parse(ev.data); } catch { return; }
      if (env?.t !== "res" || !env.cid) return;
      const resolve = this.pending.get(env.cid);
      if (resolve) {
        this.pending.delete(env.cid);
        resolve(env);
      }
    });

    const drop = () => {
      if (this.agent === ws) this.agent = null;
      // In-flight requests will reject on their own timeout.
    };
    ws.addEventListener("close", drop);
    ws.addEventListener("error", drop);
  }
}

// ── device-JWT verification (Mac → /agent) ────────────────────────────────────
interface DeviceClaims {
  sub: string; // lowercased email — the routing key
  jti?: string;
}

/**
 * Verify an HS256 device token. Returns the claims, or null on ANY failure.
 *
 * Hard rules (security invariant #2): pinned alg HS256 (reject `alg:none` and any
 * other alg), valid HMAC signature over `RELAY_JWT_SECRET`, `aud == "relay-agent"`,
 * a non-empty `sub`, and `exp` (if present) not in the past. `exp` is optional —
 * device tokens are long-lived; revocation is handled by the KV denylist, not expiry.
 */
async function verifyDeviceJWT(token: string, secret: string): Promise<DeviceClaims | null> {
  if (!secret) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [h, p, sig] = parts;

  let header: { alg?: string; typ?: string };
  let payload: { sub?: unknown; aud?: unknown; exp?: unknown; jti?: unknown };
  try {
    header = JSON.parse(b64urlToString(h));
    payload = JSON.parse(b64urlToString(p));
  } catch {
    return null;
  }
  if (header?.alg !== "HS256") return null; // pin the algorithm — reject none/RS256/etc.
  if (header?.typ && header.typ !== "JWT") return null;

  let ok = false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"],
    );
    ok = await crypto.subtle.verify(
      "HMAC",
      key,
      b64urlToBytes(sig),
      new TextEncoder().encode(`${h}.${p}`),
    );
  } catch {
    return null;
  }
  if (!ok) return null;

  if (payload?.aud !== DEVICE_AUD) return null;
  if (typeof payload?.sub !== "string" || !payload.sub) return null;
  if (payload.exp != null && Math.floor(Date.now() / 1000) > Number(payload.exp)) return null;

  return {
    sub: payload.sub.toLowerCase(),
    jti: typeof payload.jti === "string" ? payload.jti : undefined,
  };
}

/** A device token is revoked if its email (`sub`) or `jti` is on the KV denylist. */
async function isRevoked(env: Env, claims: DeviceClaims): Promise<boolean> {
  const lookups = [env.RELAY_KV.get(`revoked:sub:${claims.sub}`)];
  if (claims.jti) lookups.push(env.RELAY_KV.get(`revoked:jti:${claims.jti}`));
  const hits = await Promise.all(lookups);
  return hits.some((v) => v !== null);
}

function b64urlToBytes(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function b64urlToString(s: string): string {
  return new TextDecoder().decode(b64urlToBytes(s));
}

// ── base64 <-> ArrayBuffer (Workers have btoa/atob, not Buffer) ────────────────
function b64encode(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

function b64decode(s: string): ArrayBuffer {
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}
