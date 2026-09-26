// Košík on bunny.net: pages + JSON API in one Edge Script, data in Bunny Database.
import * as BunnySDK from "@bunny.net/edgescript-sdk";
import { checkPassword, clearCookie, isAuthed, isJobCall, sessionCookie } from "./auth.ts";
import { continueRun, latestRun, listSchedules, mikkuKeyShape, type Phase, setupSchedule, startRun } from "./jobs.ts";
import { env } from "./env.ts";
import { cartItems, HttpError, matrix, products, savedList, savedLists } from "./read.ts";
import { link, lookup, search, suggest } from "./match.ts";
import { scenarios } from "./scenarios.ts";
import { cartMove, cartRemove, cartSave, cartToggle, cartUpdate, eanDismiss, productAdd, productDelete,
  productUpdate, savedListDelete, savedListReuse, unlink } from "./write.ts";
import { PAGES } from "./pages.gen.ts";

const JOB_PHASE: Record<string, Phase> = { "/api/crawl": "kupi", "/api/eans": "eans", "/api/refresh": "stores" };
const SOURCES = ["globus", "kosik", "tesco", "billa", "dm", "rossmann", "teta", "makro", "rohlik", "kupi"];

const json = (data: unknown, status = 200, headers: HeadersInit = {}) =>
  Response.json(data, { status, headers: { "Cache-Control": "no-store", ...headers } });

/** The static site (SITE_URL) calls this API cross-origin with the session cookie. */
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
  // ---- jobs: called by Mikku (or by hand) with the job secret, not the cookie
  if (path.startsWith("/jobs/")) {
    if (!(await isJobCall(req))) return json({ error: "forbidden" }, 403);
    const chain = url.searchParams.get("chain") !== "0";  // chain=0: the caller drives the slices (no Mikku)
    if (path === "/jobs/weekly" && req.method === "POST") return json(await startRun(["stores", "kupi", "eans"], chain ? "weekly" : "manual, driven by caller", chain));
    if (path === "/jobs/continue" && req.method === "POST") {
      return json(await continueRun(Number(url.searchParams.get("run")), Number(url.searchParams.get("step")), chain));
    }
    if (path === "/jobs/setup" && req.method === "POST") return json(await setupSchedule(url.searchParams.get("cron") ?? undefined));
    if (path === "/jobs/schedules") return json(await listSchedules());
    if (path === "/jobs/status") return json({ run: await latestRun(), mikku_key: mikkuKeyShape() });
    return json({ error: "no such job endpoint" }, 404);
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
      case "/api/crawl": case "/api/eans": case "/api/refresh": {  // status of the latest run with that phase
        const r = await latestRun(JOB_PHASE[path]);
        return json({ running: !!r?.running, log: r ? r.log + (r.stale ? "\n(stopped: no progress for 20 min)" : "") : "" });
      }
      case "/api/search": {
        const n = Math.max(1, Math.min(Number(url.searchParams.get("n") ?? 60), 150));
        return json({ candidates: await search(url.searchParams.get("source") ?? "", url.searchParams.get("q") ?? "", n), n });
      }
      case "/api/scenarios": {
        const saved = url.searchParams.get("saved");
        return json(await scenarios({ saved: saved ? Number(saved) : undefined, flyers: url.searchParams.get("flyers") !== "0" }));
      }
      case "/api/suggest": return json({ results: await suggest(Number(url.searchParams.get("product_id"))) });
      case "/api/lookup": return json({ results: await lookup(url.searchParams.get("q") ?? "") });
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
      case "/api/cart/move": return json({ ok: true, moved: await cartMove(a.moves) });
      case "/api/cart/update": await cartUpdate(a.id, a.qty, a.checked); return json({ ok: true });
      case "/api/cart/remove": await cartRemove(a.id, !!a.checked_only, !!a.all); return json({ ok: true });
      case "/api/cart/save": return json({ ok: true, id: await cartSave(a.name) });
      case "/api/lists/reuse": return json({ ok: true, added: await savedListReuse(a.id) });
      case "/api/lists/delete": await savedListDelete(a.id); return json({ ok: true });
      case "/api/product": return json({ ok: true, id: await productAdd(a) });
      case "/api/product/update": await productUpdate(a.id, a); return json({ ok: true });
      case "/api/product/delete": await productDelete(a.id); return json({ ok: true });
      case "/api/eans/dismiss": await eanDismiss(a.product_id); return json({ ok: true });
      case "/api/crawl": case "/api/eans": case "/api/refresh":
        return json({ ok: true, ...(await startRun([JOB_PHASE[path]], "manual")) });
      case "/api/link": return json({ ok: true, offers: await link(a.source, a.ref, a.product_id, a.name, a.packs, a.qty) });
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
