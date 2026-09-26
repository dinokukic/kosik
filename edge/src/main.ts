// Košík on bunny.net: pages + JSON API in one Edge Script, data in Bunny Database.
import * as BunnySDK from "@bunny.net/edgescript-sdk";
import { checkPassword, clearCookie, isAuthed, sessionCookie } from "./auth.ts";
import { cartItems, HttpError, matrix, products, savedList, savedLists } from "./read.ts";
import { PAGES } from "./pages.gen.ts";

const SOURCES = ["globus", "kosik", "tesco", "billa", "dm", "rossmann", "teta", "makro", "rohlik", "kupi"];

const json = (data: unknown, status = 200, headers: HeadersInit = {}) =>
  Response.json(data, { status, headers: { "Cache-Control": "no-store", ...headers } });

const LOGIN = `<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Košík login</title>
<link rel="stylesheet" href="style.css"></head><body><div class="wrap">
<header><h1>Košík<small>household price tracker</small></h1></header>
<form method="post" action="/api/login" class="login">
  <label>Password<input type="password" name="password" autofocus autocomplete="current-password"></label>
  <button class="btn">Sign in</button> <span class="status err">__ERR__</span>
</form></div></body></html>`;

function page(path: string): Response | null {
  const body = PAGES[path];
  if (body === undefined) return null;
  const type = path.endsWith(".css") ? "text/css; charset=utf-8" : "text/html; charset=utf-8";
  return new Response(body, { headers: { "Content-Type": type, "Cache-Control": "no-cache" } });
}

async function route(req: Request): Promise<Response> {
  const url = new URL(req.url);
  const path = url.pathname;

  // ---- open: login, logout, the stylesheet
  if (path === "/login") return new Response(LOGIN.replace("__ERR__", url.searchParams.has("bad") ? "Wrong password." : ""),
    { headers: { "Content-Type": "text/html; charset=utf-8" } });
  if (path === "/api/login" && req.method === "POST") {
    const form = await req.formData().catch(() => null);
    const ok = await checkPassword(String(form?.get("password") ?? ""));
    await new Promise((r) => setTimeout(r, ok ? 0 : 800));  // slow down guessing
    return ok
      ? new Response(null, { status: 303, headers: { Location: "/", "Set-Cookie": await sessionCookie() } })
      : new Response(null, { status: 303, headers: { Location: "/login?bad" } });
  }
  if (path === "/logout") return new Response(null, { status: 303, headers: { Location: "/login", "Set-Cookie": clearCookie } });
  if (path === "/style.css") return page(path)!;

  // ---- everything else needs the session cookie
  if (!(await isAuthed(req))) {
    return path.startsWith("/api/") ? json({ error: "not signed in" }, 401)
      : new Response(null, { status: 303, headers: { Location: "/login" } });
  }

  if (!path.startsWith("/api/")) return page(path) ?? new Response("not found", { status: 404 });

  // ---- API (read side)
  if (req.method === "GET") {
    switch (path) {
      case "/api/matrix": return json(await matrix());
      case "/api/products": return json({ products: await products(), sources: SOURCES });
      case "/api/cart": return json({ items: await cartItems() });
      case "/api/lists": return json({ lists: await savedLists() });
      case "/api/list": return json(await savedList(Number(url.searchParams.get("id"))));
      case "/api/crawl": case "/api/eans": return json({ running: false, log: "" });  // jobs run via Mikku
    }
  }
  if (req.method === "POST" && !(req.headers.get("content-type") ?? "").startsWith("application/json")) {
    return json({ error: "json only" }, 415);  // a cross-site form can't send JSON without a preflight
  }
  return json({ error: `${req.method} ${path} is not on the edge yet` }, 501);
}

BunnySDK.net.http.serve(async (req: Request) => {
  try {
    const res = await route(req);
    // responses depend on who's signed in: never let the CDN cache them (pages re-validate)
    if (!res.headers.has("Cache-Control")) res.headers.set("Cache-Control", "no-store");
    return res;
  } catch (e) {
    if (e instanceof HttpError) return json({ error: e.message }, e.status);
    console.error(e);
    return json({ error: String((e as Error)?.message ?? e) }, 500);
  }
});
