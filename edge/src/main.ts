// Košík on bunny.net: pages + JSON API in one Edge Script, data in Bunny Database.
import * as BunnySDK from "@bunny.net/edgescript-sdk";
import { checkPassword, clearCookie, isAuthed, sessionCookie } from "./auth.ts";
import { env } from "./env.ts";
import { cartItems, HttpError, matrix, products, savedList, savedLists } from "./read.ts";
import { cartRemove, cartSave, cartToggle, cartUpdate, eanDismiss, productAdd, productDelete,
  productUpdate, savedListDelete, savedListReuse, unlink } from "./write.ts";
import { PAGES } from "./pages.gen.ts";

const SOURCES = ["globus", "kosik", "tesco", "billa", "dm", "rossmann", "teta", "makro", "rohlik", "kupi"];

const json = (data: unknown, status = 200, headers: HeadersInit = {}) =>
  Response.json(data, { status, headers: { "Cache-Control": "no-store", ...headers } });

/** The static site (kosik.ulandino.com) calls this API cross-origin with the session cookie. */
const siteOrigin = () => (env("SITE_URL") ?? "").replace(/\/$/, "");
function cors(req: Request, res: Response): Response {
  const origin = req.headers.get("origin");
  if (origin && origin === siteOrigin()) {
    res.headers.set("Access-Control-Allow-Origin", origin);
    res.headers.set("Access-Control-Allow-Credentials", "true");
    res.headers.append("Vary", "Origin");
  }
  return res;
}
/** Where to send the browser back to: the site if the request came from it, else this host. */
const back = (req: Request, path: string) =>
  (req.headers.get("origin") === siteOrigin() && siteOrigin() ? siteOrigin() : "") + path;

function page(path: string): Response | null {
  const body = PAGES[path];
  if (body === undefined) return null;
  const type = path.endsWith(".css") ? "text/css; charset=utf-8"
    : path.endsWith(".js") ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8";
  return new Response(body, { headers: { "Content-Type": type, "Cache-Control": "no-cache" } });
}

async function route(req: Request): Promise<Response> {
  const url = new URL(req.url);
  const path = url.pathname;

  // ---- CORS preflight from the static site
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: {
      "Access-Control-Allow-Methods": "GET, POST", "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400" } });
  }
  // ---- open: login page + its assets, login, logout
  if (path === "/login") return page("/login.html")!;
  if (["/login.html", "/style.css", "/config.js"].includes(path)) return page(path)!;
  if (path === "/api/login" && req.method === "POST") {
    const form = await req.formData().catch(() => null);
    const ok = await checkPassword(String(form?.get("password") ?? ""));
    await new Promise((r) => setTimeout(r, ok ? 0 : 800));  // slow down guessing
    return ok
      ? new Response(null, { status: 303, headers: { Location: back(req, "/"), "Set-Cookie": await sessionCookie() } })
      : new Response(null, { status: 303, headers: { Location: back(req, "/login.html?bad") } });
  }
  if (path === "/logout") {
    const to = siteOrigin() && (req.headers.get("referer") ?? "").startsWith(siteOrigin()) ? siteOrigin() : "";
    return new Response(null, { status: 303, headers: { Location: to + "/login.html", "Set-Cookie": clearCookie } });
  }

  // ---- everything else needs the session cookie
  if (!(await isAuthed(req))) {
    return path.startsWith("/api/") ? json({ error: "not signed in" }, 401)
      : new Response(null, { status: 303, headers: { Location: "/login.html" } });
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
  if (req.method === "POST") {
    // deno-lint-ignore no-explicit-any
    const a: any = await req.json().catch(() => ({}));
    switch (path) {
      case "/api/cart/toggle": return json({ ok: true, in_cart: await cartToggle(a.row_key, a.store_id, a.kind ?? "store") });
      case "/api/cart/update": await cartUpdate(a.id, a.qty, a.checked); return json({ ok: true });
      case "/api/cart/remove": await cartRemove(a.id, !!a.checked_only, !!a.all); return json({ ok: true });
      case "/api/cart/save": return json({ ok: true, id: await cartSave(a.name) });
      case "/api/lists/reuse": return json({ ok: true, added: await savedListReuse(a.id) });
      case "/api/lists/delete": await savedListDelete(a.id); return json({ ok: true });
      case "/api/product": return json({ ok: true, id: await productAdd(a) });
      case "/api/product/update": await productUpdate(a.id, a); return json({ ok: true });
      case "/api/product/delete": await productDelete(a.id); return json({ ok: true });
      case "/api/eans/dismiss": await eanDismiss(a.product_id); return json({ ok: true });
      case "/api/unlink": return json({ ok: true, removed: await unlink(a.source, a.ref, a.product_id) });
    }
  }
  return json({ error: `${req.method} ${path} is not on the edge yet` }, 501);
}

BunnySDK.net.http.serve(async (req: Request) => {
  try {
    const res = cors(req, await route(req));
    // responses depend on who's signed in: never let the CDN cache them (pages re-validate)
    if (!res.headers.has("Cache-Control")) res.headers.set("Cache-Control", "no-store");
    return res;
  } catch (e) {
    if (e instanceof HttpError) return cors(req, json({ error: e.message }, e.status));
    console.error(e);
    return cors(req, json({ error: String((e as Error)?.message ?? e) }, 500));
  }
});
