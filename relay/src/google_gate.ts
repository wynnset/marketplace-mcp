/**
 * google_gate.ts — the human gate for the relay's OAuth authorization server.
 *
 * 1:1 port of the Google sign-in + email-allowlist check from `src/oauth.py`. The
 * OAuth *protocol* (DCR, PKCE, /token, /register, /revoke, the metadata docs, and
 * the RFC 9728 `WWW-Authenticate` header claude.ai requires) is handled by
 * `@cloudflare/workers-oauth-provider`; this module only decides *who* is allowed:
 *
 *     claude.ai ──/authorize──▶ this Worker ──redirect──▶ Google sign-in
 *               ◀── token ────  ◀── /oauth/google/callback (verify email ∈ allowlist)
 *
 * The access token claude.ai ends up with is the library's own opaque, KV-backed
 * token (NOT a JWT) — see docs/RELAY_MIGRATION.md "Token strategy". We never sign
 * anything here; we just gate `completeAuthorization`.
 */

export const GOOGLE_CALLBACK_PATH = "/oauth/google/callback";

// Google OIDC endpoints. Overridable via env vars so tests can point the gate at a
// stub OIDC server (mirrors oauth.py's "overridable as instance attrs for testing").
const DEFAULT_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const DEFAULT_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
const DEFAULT_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo";

export interface GoogleGateEnv {
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  MCP_ALLOWED_EMAILS: string;
  PUBLIC_URL: string;
  // optional test overrides
  GOOGLE_AUTH_URL?: string;
  GOOGLE_TOKEN_URL?: string;
  GOOGLE_USERINFO_URL?: string;
}

function publicUrl(env: GoogleGateEnv): string {
  return env.PUBLIC_URL.replace(/\/+$/, "");
}

export function redirectUri(env: GoogleGateEnv): string {
  return publicUrl(env) + GOOGLE_CALLBACK_PATH;
}

export function allowedEmails(env: GoogleGateEnv): Set<string> {
  return new Set(
    (env.MCP_ALLOWED_EMAILS || "")
      .split(",")
      .map((e) => e.trim().toLowerCase())
      .filter(Boolean),
  );
}

/** Build the Google sign-in URL to bounce the browser to (mirrors oauth.py authorize). */
export function buildGoogleAuthUrl(env: GoogleGateEnv, state: string): string {
  const base = env.GOOGLE_AUTH_URL || DEFAULT_GOOGLE_AUTH_URL;
  const q = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    redirect_uri: redirectUri(env),
    response_type: "code",
    scope: "openid email",
    state,
    access_type: "online",
    prompt: "select_account",
  });
  return `${base}?${q.toString()}`;
}

/**
 * Exchange the Google auth code and return (email, email_verified).
 *
 * We trust the userinfo response because the access token came straight from
 * Google's token endpoint over TLS using our own client secret — no separate JWT
 * signature check is needed for that path (same reasoning as oauth.py._fetch_email).
 */
export async function fetchGoogleEmail(
  env: GoogleGateEnv,
  code: string,
): Promise<{ email: string; verified: boolean }> {
  const tokenUrl = env.GOOGLE_TOKEN_URL || DEFAULT_GOOGLE_TOKEN_URL;
  const userinfoUrl = env.GOOGLE_USERINFO_URL || DEFAULT_GOOGLE_USERINFO_URL;

  const tokRes = await fetch(tokenUrl, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code,
      client_id: env.GOOGLE_CLIENT_ID,
      client_secret: env.GOOGLE_CLIENT_SECRET,
      redirect_uri: redirectUri(env),
      grant_type: "authorization_code",
    }),
  });
  if (!tokRes.ok) throw new Error(`google token exchange failed: ${tokRes.status}`);
  const tok = (await tokRes.json()) as { access_token?: string };
  if (!tok.access_token) throw new Error("google token exchange: no access_token");

  const infoRes = await fetch(userinfoUrl, {
    headers: { authorization: `Bearer ${tok.access_token}` },
  });
  if (!infoRes.ok) throw new Error(`google userinfo failed: ${infoRes.status}`);
  const info = (await infoRes.json()) as { email?: string; email_verified?: boolean };
  return { email: info.email || "", verified: Boolean(info.email_verified) };
}

// ── minimal styled result pages (ported from oauth.py) ─────────────────────────
const STYLE =
  "font-family:-apple-system,system-ui,sans-serif;max-width:24rem;margin:18vh auto;" +
  "padding:2rem;border:1px solid #e5e5e5;border-radius:12px;text-align:center";

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

export function denyPage(message: string): Response {
  const body = `<!doctype html><meta charset=utf-8><title>Access denied</title>
<div style="${STYLE}"><h2>Access denied</h2><p>${escapeHtml(message)}</p>
<p style="color:#888;font-size:.85rem">Close this window and try again from claude.ai.</p></div>`;
  return new Response(body, { status: 403, headers: { "content-type": "text/html; charset=utf-8" } });
}

export function expiredPage(): Response {
  const body = `<!doctype html><meta charset=utf-8><title>Request expired</title>
<div style="${STYLE}"><h2>Request expired</h2>
<p>This authorization request is no longer valid. Start over from claude.ai.</p></div>`;
  return new Response(body, { status: 400, headers: { "content-type": "text/html; charset=utf-8" } });
}
