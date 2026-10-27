type Bindings = { DB?: D1Database; SYNC_TOKEN?: string };
const headers = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow" };
const json = (value: unknown, status = 200) => Response.json(value, { status, headers });
export function validTarget(value: string) {
  try { const u = new URL(value); return u.protocol === "https:" && ["chat.whatsapp.com", "wa.me", "api.whatsapp.com"].includes(u.hostname) && !u.username && !u.password; } catch { return false; }
}
async function authorized(request: Request, token?: string) {
  if (!token) return false;
  const hash = async (s: string) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([hash(request.headers.get("authorization") || ""), hash(`Bearer ${token}`)]);
  return a.reduce((diff, n, i) => diff | (n ^ b[i]), 0) === 0;
}
export async function serveJoin(request: Request, env: Bindings): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname === "/health") return json({ ok: true });
  if (url.pathname.startsWith("/api/sync/")) {
    if (!await authorized(request, env.SYNC_TOKEN)) return json({ error: "Unauthorized" }, 401);
    if (!env.DB) return json({ error: "Storage unavailable" }, 503);
    if (url.pathname === "/api/sync/links" && request.method === "POST") {
      if (Number(request.headers.get("content-length")) > 200000) return json({ error: "Too large" }, 413);
      const body = await request.json() as { links: {code:string;target:string;state:string}[] };
      if (!Array.isArray(body.links) || body.links.length > 100 || body.links.some(l => !/^[A-Za-z0-9_-]{5,80}$/.test(l.code) || !validTarget(l.target) || !["active", "used", "expired"].includes(l.state))) return json({ error: "Invalid links" }, 400);
      if (body.links.length) await env.DB.batch(body.links.map(l => env.DB!.prepare("INSERT INTO join_links(code,target,state,updated) VALUES(?,?,?,?) ON CONFLICT(code) DO UPDATE SET target=excluded.target,state=excluded.state,updated=excluded.updated").bind(l.code,l.target,l.state,new Date().toISOString())));
      return json({ ok: true });
    }
    if (url.pathname === "/api/sync/events" && request.method === "GET") {
      const after = Number(url.searchParams.get("after") || 0);
      if (!Number.isSafeInteger(after) || after < 0) return json({ error: "Invalid cursor" }, 400);
      const result = await env.DB.prepare("SELECT id,code,at,ua,ip FROM join_events WHERE id>? ORDER BY id LIMIT 500").bind(after).all();
      return json({ events: result.results });
    }
    return json({ error: "Not found" },404);
  }
  const match = /^\/j\/([A-Za-z0-9_-]{5,80})$/.exec(url.pathname);
  if (!match) return null;
  if (!["GET", "HEAD"].includes(request.method)) return new Response(null, { status:405, headers });
  const link = await env.DB?.prepare("SELECT target,state FROM join_links WHERE code=?").bind(match[1]).first<{target:string;state:string}>();
  if (!link) return new Response("This invite is incomplete. Please ask Kalakriti Studio for a fresh workshop link.", { status:404, headers });
  if (link.state === "expired") return new Response("This invite is for an earlier workshop. Please DM Kalakriti Studio for the new workshop link.", {status:410,headers});
  const ua = request.headers.get("user-agent") || "";
  if (request.method === "GET" && link.state === "active" && !/facebookexternalhit|facebot|whatsapp|bot|crawler|preview/i.test(ua)) {
    try {
      await env.DB!.prepare("INSERT INTO join_events(code,at,ua,ip) VALUES(?,?,?,?)").bind(match[1],new Date().toISOString(),ua.slice(0,2048),(request.headers.get("cf-connecting-ip") || "").slice(0,64)).run();
    } catch { console.error("Join event storage failed"); /* Keep the invite usable during a storage write failure. */ }
  }
  return new Response(null, { status:302, headers:{...headers, Location:link.target} });
}
