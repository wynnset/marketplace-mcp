/**
 * marketplace-relay — Cloudflare Worker + Durable Object  (STEP 1 SKELETON)
 *
 * The single public endpoint for every friend's Mac. One URL, no per-friend
 * subdomain: requests are routed to the right Mac by *identity*, not by hostname.
 *
 *   claude.ai ──POST /mcp──▶ Worker ─▶ DO(identity) ─┐
 *   Alice Mac ──WSS /agent─▶ Worker ─▶ DO(identity) ◀┘ (holds the live socket)
 *
 * A Durable Object instance is created per identity (`idFromName(identity)`); it
 * is the rendezvous point where claude.ai's request meets the Mac's registered
 * WebSocket. Because every MCP tool call is a single JSON response (the Mac runs
 * stateless_http + json_response), the DO is a plain request/response proxy with
 * correlation ids — no streaming to keep alive.
 *
 * STEP 1 stubs identity as the `X-Relay-Identity` header. Step 2 issues signed JWT
 * access tokens and Step 3 derives identity from the verified token `sub` (for /mcp)
 * and a signed device-JWT `sub` (for /agent) — same routing, real auth.
 */

export interface Env {
  RELAY: DurableObjectNamespace;
}

const IDENTITY_HEADER = "x-relay-identity";
const AGENT_TIMEOUT_MS = 30_000;

function identityOf(req: Request): string {
  // STEP 1 stub. Step 3: verify JWT and use its `sub`.
  return req.headers.get(IDENTITY_HEADER) ?? "default";
}

function rpcError(message: string, code: number, httpStatus: number): Response {
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", error: { code, message }, id: null }),
    { status: httpStatus, headers: { "content-type": "application/json" } },
  );
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/healthz") {
      return new Response("ok\n", { headers: { "content-type": "text/plain" } });
    }

    // Mac agent registers here (outbound WebSocket).
    if (url.pathname === "/agent") {
      if (req.headers.get("Upgrade") !== "websocket") {
        return new Response("expected websocket upgrade", { status: 426 });
      }
      const id = env.RELAY.idFromName(identityOf(req));
      return env.RELAY.get(id).fetch(req);
    }

    // claude.ai speaks MCP here. Routed to the same DO as the matching agent.
    if (url.pathname === "/mcp") {
      const id = env.RELAY.idFromName(identityOf(req));
      return env.RELAY.get(id).fetch(req);
    }

    return new Response("not found", { status: 404 });
  },
};

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
        headers: [...req.headers].filter(([k]) => k !== IDENTITY_HEADER),
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
