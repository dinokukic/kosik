// Background work on the edge, run weekly by Mikku (mikku.dev) and on demand from the match screen.
// A run = phases (stores -> kupi -> eans), done in slices that fit one Edge Script request
// (<= 50 subrequests, every DB call counts; ~30 s CPU). After each slice the script asks Mikku to
// call /jobs/continue a moment later; a step is claimed atomically, so Mikku's retries and
// duplicate deliveries can't run a slice twice. Progress + log live in job_run (Bunny Database).
import { all, db, first, Row, run } from "./db.ts";
import { env, must } from "./env.ts";
import { dealStatements, offerStatements } from "./match.ts";
import { searchText } from "./read.ts";
import { Kupi, source, type Offer } from "./stores.ts";
import { inferUnit, today } from "./write.ts";

export type Phase = "stores" | "kupi" | "eans";
const ALL_STORE_SOURCES = ["globus", "kosik", "tesco", "billa", "dm", "rossmann", "teta", "makro", "rohlik"];
/** Tesco needs its web API key; without it the store is skipped instead of failing every run. */
const storeSources = () => ALL_STORE_SOURCES.filter((s) => s !== "tesco" || !!env("TESCO_XAPI_KEY"));
/** With a Mikku key, runs chain themselves in the background; without one, the open page drives them. */
export const mikkuOn = () => !!env("MIKKU_API_KEY")?.trim();
// refs per store call (batched APIs) and store calls per ref (per-item APIs)
const CHUNK: Record<string, number> = { globus: 50, tesco: 10, billa: 30, dm: 30, rohlik: 20, kosik: 8, rossmann: 8, teta: 8, makro: 6 };
const CALLS_PER_CHUNK: Record<string, number> = { kosik: 8, rossmann: 8, teta: 8, makro: 12 };
const CRAWL_CHAINS = ["albert", "billa", "tesco", "kaufland", "lidl", "penny-market", "globus", "makro", "kosik-cz", "flop",
  "flop-top", "cba", "coop", "hruska", "jip", "eso-market", "ratio", "dm-drogerie", "teta-drogerie"];
const KUPI_STORE: Record<string, string> = { "penny-market": "penny", "teta-drogerie": "teta", "dm-drogerie": "dm" };
const PAGES_PER_SLICE = 12, EANS_PER_SLICE = 3, WALL_MS = 20000, BUDGET = 40;
const STALE_MS = 20 * 60 * 1000;  // a run with no progress for 20 min is dead (lost continuation)

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const nowIso = () => new Date().toISOString();
const stmt = (sql: string, ...args: unknown[]) => db().prepare(sql).bind(...(args.map((a) => a === undefined ? null : a) as never[]));

/** Counts subrequests (store calls + DB round trips) and wall time for one slice. */
class Budget {
  used = 0; t0 = Date.now();
  constructor(public limit = BUDGET) {}
  take(n = 1) { this.used += n; }
  left() { return this.limit - this.used; }
  ok(need = 1) { return this.used + need <= this.limit && Date.now() - this.t0 < WALL_MS; }
}

// ---------------------------------------------------------------- run bookkeeping
export async function latestRun(phase?: Phase): Promise<(Row & { running: boolean; stale: boolean }) | null> {
  const runs = await all("SELECT * FROM job_run ORDER BY id DESC LIMIT 10");
  const r = phase ? runs.find((x) => JSON.parse(x.phases).includes(phase)) : runs[0];
  if (!r) return null;
  const stale = r.status === "running" && Date.now() - Date.parse(r.updated) > STALE_MS;
  return { ...r, running: r.status === "running" && !stale, stale };
}

/** Start a run (unless one is already going) and do its first slice right away. */
export async function startRun(phases: Phase[], trigger: string, chain = true) {
  const cur = await latestRun();
  if (cur?.running) return { started: false, run: cur.id, note: "a run is already in progress" };
  const r = await run("INSERT INTO job_run(phases,status,step,claimed,state,log,started,updated) VALUES (?,'running',0,0,'{}',?,?,?)",
    JSON.stringify(phases), `${nowIso().slice(0, 16)} started (${trigger}): ${phases.join(" → ")}\n`, nowIso(), nowIso());
  const id = Number(r.lastInsertRowid);
  if (!chain) return { started: true, run: id };
  try {
    await scheduleContinue(id, 1, 0);  // every slice, the first included, arrives via Mikku
  } catch (e) {  // nothing will ever continue this run: close it instead of blocking the next one
    await run("UPDATE job_run SET status='failed', log=log || ?, updated=? WHERE id=?", `could not queue the first step: ${String((e as Error)?.message ?? e)}\n`, nowIso(), id);
    throw e;
  }
  return { started: true, run: id };
}

/** Do slice `step` of run `id` (called by startRun and by Mikku via /jobs/continue). */
export async function continueRun(id: number, step: number, chain = true) {
  // claim the step: only one delivery gets to work on it
  const claim = await run("UPDATE job_run SET claimed=?, updated=? WHERE id=? AND status='running' AND step=? AND claimed<?",
    step, nowIso(), id, step - 1, step);
  if (!claim.rowsAffected) return { skipped: true };
  const job = (await first("SELECT * FROM job_run WHERE id=?", id))!;
  const phases: Phase[] = JSON.parse(job.phases);
  const state = JSON.parse(job.state);
  const budget = new Budget();
  budget.take(2);  // the claim + the read above
  const lines: string[] = [];
  try {
    let pi = state.pi ?? 0;
    while (pi < phases.length && budget.ok(4)) {
      const done = await PHASES[phases[pi]](state, budget, lines);
      if (!done) break;
      pi++; state.pi = pi;
    }
    const finished = pi >= phases.length;
    if (finished) lines.push(`weekly run done: ${summary(state)}`);
    await db().batch([stmt("UPDATE job_run SET step=?, state=?, status=?, log=substr(log || ?, -6000), updated=? WHERE id=?",
      step, JSON.stringify(state), finished ? "done" : "running", lines.map((l) => `${nowIso().slice(11, 16)} ${l}\n`).join(""), nowIso(), id)]);
    if (!finished && chain) await scheduleContinue(id, step + 1);  // chain=false: the caller drives the slices
    return { run: id, step, finished, did: lines };
  } catch (e) {
    // release the claim so Mikku's retry of this step can pick it up again
    await run("UPDATE job_run SET claimed=?, log=substr(log || ?, -6000), updated=? WHERE id=?", step - 1,
      `${nowIso().slice(11, 16)} step ${step} failed (will retry): ${String((e as Error)?.message ?? e)}\n`, nowIso(), id);
    throw e;
  }
}

function summary(s: Row) {
  return `store prices ${s.stores?.saved ?? 0} offers for ${s.stores?.refs ?? 0} links · ` +
    `flyers ${s.kupi?.offers ?? 0} offers from ${s.kupi?.chainsDone ?? 0} chains · barcodes looked up for ${s.eans?.done ?? 0} items`;
}

const mikkuKey = () => must("MIKKU_API_KEY").trim();
/** Shape of the stored Mikku key, never the key: to debug "invalid api key" without revealing it. */
export function mikkuKeyShape() {
  const raw = env("MIKKU_API_KEY") ?? "";
  return { set: !!raw, looks_like_mk_live: /^mk_live_/.test(raw.trim()), length: raw.trim().length, has_whitespace: raw !== raw.trim() || /\s/.test(raw.trim()) };
}

/** Ask Mikku to call /jobs/continue for the next slice (idempotent per run+step). */
async function scheduleContinue(id: number, step: number, delaySeconds = 2) {
  const api = must("API_URL").replace(/\/$/, "");
  const r = await fetch("https://mikku.dev/v1/messages", {
    method: "POST",
    headers: { "Authorization": `Bearer ${mikkuKey()}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      url: `${api}/jobs/continue?run=${id}&step=${step}`, method: "POST", body: "{}",
      headers: { "Authorization": `Bearer ${must("JOB_SECRET")}`, "Content-Type": "application/json" },
      delaySeconds, maxAttempts: 5, retryDelaySeconds: 30, backoffStrategy: "exponential", timeoutSeconds: 120,
      idempotencyKey: `kosik-run-${id}-step-${step}`,
    }),
  });
  if (!r.ok) throw new Error(`Mikku publish failed: HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
}

/** Mikku schedules (name, cron, next run) — never their headers, which hold the job secret. */
export async function listSchedules() {
  const r = await fetch("https://mikku.dev/v1/schedules", { headers: { "Authorization": `Bearer ${mikkuKey()}` } });
  const d = await r.json();
  // deno-lint-ignore no-explicit-any
  return ((d.data ?? d.schedules ?? d ?? []) as any[]).map((s) => ({ id: s.id, name: s.name, cron: s.cron, enabled: s.enabled, nextRunAt: s.nextRunAt, url: s.url }));
}

/** Create (or replace) the weekly Mikku schedule: Fridays 13:00 UTC (15:00 CEST / 14:00 CET). */
export async function setupSchedule(cron = "0 13 * * 5") {
  const key = mikkuKey(), api = must("API_URL").replace(/\/$/, "");
  const h = { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" };
  const list = await (await fetch("https://mikku.dev/v1/schedules", { headers: h })).json();
  // deno-lint-ignore no-explicit-any
  for (const s of (list.data ?? list.schedules ?? list ?? []) as any[]) {
    if (s?.name === "kosik-weekly") await fetch(`https://mikku.dev/v1/schedules/${s.id}`, { method: "DELETE", headers: h });
  }
  const r = await fetch("https://mikku.dev/v1/schedules", { method: "POST", headers: h, body: JSON.stringify({
    name: "kosik-weekly", cron, url: `${api}/jobs/weekly`, method: "POST", body: "{}", enabled: true,
    headers: { "Authorization": `Bearer ${must("JOB_SECRET")}`, "Content-Type": "application/json" },
    maxAttempts: 5, retryDelaySeconds: 60, backoffStrategy: "exponential", timeoutSeconds: 120 }) });
  return { status: r.status, body: await r.json().catch(() => null) };
}

// ---------------------------------------------------------------- phases (return true when done)
type PhaseFn = (state: Row, budget: Budget, log: string[]) => Promise<boolean>;

/** Store prices: every watch of a direct store, in the store's batch size. */
const storesPhase: PhaseFn = async (state, budget, log) => {
  const st = state.stores ??= { si: 0, offset: 0, saved: 0, refs: 0 };
  const STORE_SOURCES = storeSources();
  while (st.si < STORE_SOURCES.length) {
    const src = STORE_SOURCES[st.si];
    const watches = await all("SELECT * FROM watch WHERE source=? AND product_id IS NOT NULL ORDER BY id", src);
    budget.take();
    const size = CHUNK[src] ?? 20, calls = CALLS_PER_CHUNK[src] ?? 1;
    while (st.offset < watches.length) {
      if (!budget.ok(calls + 2)) return false;
      const chunk = watches.slice(st.offset, st.offset + size);
      const refs = [...new Set(chunk.map((w) => w.ref as string))];
      let got: Record<string, Offer[]> = {};
      try { got = await source(src).fetch(refs); }
      catch (e) { log.push(`${src}: ${String((e as Error).message ?? e)} (skipped ${refs.length})`); }
      budget.take(calls);
      const prods = new Map((await all(`SELECT * FROM product WHERE id IN (${chunk.map(() => "?").join(",")})`,
        ...chunk.map((w) => w.product_id))).map((p) => [p.id, p]));
      budget.take();
      const batch = [];
      for (const w of chunk) {
        const p = prods.get(w.product_id);
        if (!p) continue;
        const keep = w.packs ? new Set(String(w.packs).split(",")) : null;
        const offers = (got[w.ref] ?? []).filter((o) => !keep || !o.key || keep.has(o.key));
        batch.push(...offerStatements(src, offers, p, w.qty));
        if (!offers.length && Object.keys(got).length) batch.push(stmt("UPDATE offering SET active=0 WHERE source=? AND product_id=? AND store_ref=?", src, p.id, w.ref));
        st.saved += offers.length;
      }
      if (batch.length) { await db().batch(batch, { mode: "immediate" }); budget.take(); }
      st.offset += chunk.length; st.refs += chunk.length; st.ok = (st.ok ?? 0) + chunk.filter((w) => got[w.ref]?.length).length;
      st.srcOk = (st.srcOk ?? 0) + chunk.filter((w) => got[w.ref]?.length).length;
      await sleep(src === "globus" ? 3000 : 800);  // be polite (globus robots.txt: crawl-delay 3)
    }
    log.push(`${src}: ${st.srcOk ?? 0} of ${watches.length} links refreshed`);
    st.si++; st.offset = 0; st.srcOk = 0;
  }
  return true;
};

/** Kupi: every page of every tracked chain's flyer, then the watched Kupi products. */
const kupiPhase: PhaseFn = async (state, budget, log) => {
  const k = state.kupi ??= { ci: 0, page: 1, offers: 0, chainsDone: 0, watchedDone: false, chainOffers: 0 };
  let pages = 0;
  const pending: Offer[] = [];
  const flush = async () => {
    const stmts = dealStatements(pending.splice(0));
    for (let i = 0; i < stmts.length; i += 250) { await db().batch(stmts.slice(i, i + 250), { mode: "immediate" }); budget.take(); }
  };
  while (k.ci < CRAWL_CHAINS.length) {
    if (pages >= PAGES_PER_SLICE || !budget.ok(4)) { await flush(); return false; }
    const chain = CRAWL_CHAINS[k.ci];
    let res;
    try { res = await Kupi.crawlPage(chain, k.page); }
    catch (e) { log.push(`kupi ${chain} p${k.page}: ${String((e as Error).message ?? e)}`); res = { products: 0, offers: [] as Offer[], failed: true }; }
    budget.take(); pages++;
    pending.push(...res.offers); k.offers += res.offers.length; k.chainOffers += res.offers.length;
    if (!res.products || k.page >= 150) {  // chain finished
      await flush();
      // deno-lint-ignore no-explicit-any
      if (!(res as any).failed) {
        await run("INSERT OR REPLACE INTO crawl_log(store_id,ts,deals) VALUES (?,?,?)", KUPI_STORE[chain] ?? chain, today(), k.chainOffers);
        budget.take(); k.chainsDone++;
      }
      log.push(`kupi ${chain}: ${k.chainOffers} offers, ${k.page - 1} pages`);
      k.ci++; k.page = 1; k.chainOffers = 0;
    } else k.page++;
    await sleep(1200);
  }
  await flush();
  if (!k.watchedDone) {  // watched products catch chains outside the list
    const slugs = (await all("SELECT DISTINCT ref FROM watch WHERE source='kupi' ORDER BY ref")).map((r) => r.ref as string);
    budget.take();
    k.wi ??= 0;
    while (k.wi < slugs.length) {
      if (!budget.ok(3)) { await flush(); return false; }
      try { pending.push(...((await Kupi.fetch([slugs[k.wi]]))[slugs[k.wi]] ?? [])); } catch (e) { log.push(`kupi ${slugs[k.wi]}: ${e}`); }
      budget.take(); k.wi++;
      await sleep(1200);
    }
    await flush();
    await run("INSERT OR REPLACE INTO crawl_log(store_id,ts,deals) VALUES ('*',?,NULL)", today());
    budget.take();
    k.watchedDone = true;
    log.push(`kupi: ${slugs.length} watched products refreshed; ${k.offers} offers in all`);
  }
  return true;
};

// ---- barcodes by name (port of pricetrack.ean_candidates / ean_score)
const EAN_STOP = new Set(["baleni", "ks", "kus", "bio", "cca", "volne", "vanicka", "sacek", "rohlik", "cz", "the", "and",
  "s", "v", "z", "na", "pro", "a", "se", "do"]);
const LOOSE = /(?<![\p{L}\p{N}])1\s*ks(?![\p{L}\p{N}])|volně|volná|volné|volný|svazek|květináč|(?<![\p{L}\p{N}])cca(?![\p{L}\p{N}])/iu;
const tokens = (s: string) => new Set((s ?? "").toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "")
  .match(/[a-z0-9]+/g)?.filter((t) => t.length > 1 && !/^\d+$/.test(t) && !EAN_STOP.has(t)) ?? []);
const pcts = (n: string) => new Set([...n.replace(/,/g, ".").matchAll(/(\d+(?:\.\d+)?)\s*%/g)].map((m) => m[1]));

export function eanScore(p: Row, candName: string, candAmount: string): number {
  const q = searchText(p), want = tokens(q);
  if (!want.size) return 0;
  const got = tokens(candName);
  let score = [...want].filter((t) => got.has(t)).length / want.size;
  const brand = tokens(q.replace(/^(rohlik\.cz|bio)\s+/i, "").split(" ")[0]);
  if (brand.size && ![...brand].some((t) => got.has(t))) score -= 0.4;
  const ours = p.search_query || p.name, a = pcts(ours), b = pcts(candName);
  if (a.size && b.size && ![...a].some((x) => b.has(x))) score -= 0.4;  // 0 % vs 5 % yogurt
  const own = candName.trim().match(/^(tesco|globus|váš výběr|vas vyber|k-classic|clever|billa)(?![\p{L}\p{N}])/iu);
  if (own && !ours.trim().toLowerCase().startsWith(own[1].toLowerCase())) score -= 0.4;  // a chain's own brand
  const [unit, qty] = inferUnit(`${candName} ${candAmount}`);
  const same = unit === p.unit || (new Set([unit, p.unit]).size === 2 && [unit, p.unit].every((u) => u === "kg" || u === "l"));
  if (same && p.unit_qty) { const r = qty / p.unit_qty; score += r >= 0.95 && r <= 1.05 ? 0.25 : (r < 0.8 || r > 1.25 ? -0.25 : 0); }
  return Math.round(score * 1000) / 1000;
}

async function eanCandidates(p: Row, budget: Budget) {
  const q = searchText(p), words = q.split(" ");
  const queries = [q, ...(words.length > 3 ? [words.slice(0, 3).join(" ")] : [])];
  const out: Row[] = [];
  for (const name of ["globus", "tesco"]) {
    let hits: Offer[] = [];
    for (const query of queries) {  // full name first, then a shorter one if nothing came back
      try { hits = (await source(name).search(query, 12)).map((c) => c.offers[0]).filter((o) => o?.ean); } catch { hits = []; }
      budget.take();
      await sleep(name === "globus" ? 3000 : 1500);
      if (hits.length) break;
    }
    const scored = hits.map((o) => [eanScore(p, o.name, o.amount), o] as const).sort((x, y) => y[0] - x[0]);
    out.push(...scored.slice(0, 3).filter(([s]) => s >= 0.6).map(([s, o]) => ({ source: name, ean: o.ean, name: o.name, amount: o.amount, price: o.price_incl, score: s })));
  }
  const merged = new Map<string, Row>();
  for (const x of out.sort((a, b) => b.score - a.score)) {  // same EAN from both stores = one, stronger
    const m = merged.get(x.ean);
    if (m) { m.sources.push(x.source); m.score = Math.round((m.score + 0.1) * 1000) / 1000; }
    else merged.set(x.ean, { ...x, sources: [x.source] });
  }
  return [...merged.values()].sort((a, b) => b.score - a.score);
}

/** Barcode candidates for items without an EAN that were never looked up (new items). */
const eansPhase: PhaseFn = async (state, budget, log) => {
  const e = state.eans ??= { done: 0, found: 0 };
  const todo = (await all(`SELECT * FROM product WHERE ean IS NULL AND id NOT IN (SELECT product_id FROM ean_lookup) ORDER BY id`))
    .filter((p) => !LOOSE.test(p.name));
  budget.take();
  for (const p of todo.slice(0, EANS_PER_SLICE)) {
    if (!budget.ok(6)) return false;
    const cands = await eanCandidates(p, budget);
    await db().batch([
      stmt("DELETE FROM ean_candidate WHERE product_id=?", p.id),
      ...cands.slice(0, 5).map((x) => stmt("INSERT INTO ean_candidate(product_id,ean,sources,name,amount,price,score) VALUES (?,?,?,?,?,?,?)",
        p.id, x.ean, x.sources.join(","), x.name, x.amount, x.price, x.score)),
      stmt("INSERT OR REPLACE INTO ean_lookup(product_id,ts,found,dismissed) VALUES (?,?,?,0)", p.id, today(), cands.length),
    ], { mode: "immediate" });
    budget.take();
    e.done++; e.found += cands.length ? 1 : 0;
    log.push(`barcodes: ${p.name.slice(0, 40)} → ${cands.length} candidate(s)`);
  }
  return todo.length <= EANS_PER_SLICE;
};

const PHASES: Record<Phase, PhaseFn> = { stores: storesPhase, kupi: kupiPhase, eans: eansPhase };
