// Store connectors (ports of the pricetrack.py classes). Each returns candidates for a search
// and offers for known refs. Offer = one price for one listing at one store; a candidate =
// one searchable product with its current offers. All verified live from the edge 2026-09-26.
import { env } from "./env.ts";
import { inferUnit } from "./write.ts";

export type Offer = {
  store_id: string; store_name: string; store_ref: string; name: string; amount: string; url: string;
  price_incl: number; is_promo: boolean; valid_from: string | null; promo_until: string | null;
  club: string | null; note: string | null; ref: string; ean?: string | null; eans?: string[];
  price_net?: number; key?: string; product?: string; slug?: string;
};
export type Candidate = { ref: string; name: string; url: string; offers: Offer[] };
export type Source = {
  aggregator?: boolean; eanSearch?: boolean; eanViaName?: boolean;
  search(q: string, n?: number): Promise<Candidate[]>;
  fetch(refs: string[]): Promise<Record<string, Offer[]>>;
  refOf(storeRef: string): string;
};

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36";
const same = (s: string) => s;
const isEan = (q: string) => /^\d{8,14}$/.test((q ?? "").trim());

async function getJson(url: string, init: RequestInit = {}) {
  const r = await fetch(url, { ...init, headers: { "User-Agent": UA, Accept: "application/json", ...(init.headers ?? {}) },
    signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(`${new URL(url).host} HTTP ${r.status}`);
  return await r.json();
}
async function getText(url: string) {
  const r = await fetch(url, { headers: { "User-Agent": UA }, signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(`${new URL(url).host} HTTP ${r.status}`);
  return await r.text();
}
const qs = (params: [string, string | number][]) => params.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join("&");

function storeOffer(storeId: string, storeName: string, ref: string | number, name: string, amount: string, url: string,
  price: number, regular?: number | null, until?: string | null, note?: string | null, ean?: string | number | null): Offer {
  const promo = regular != null && price != null && price < regular - 0.001;
  return { store_id: storeId, store_name: storeName, store_ref: String(ref), name, amount: amount || "", url,
    price_incl: Number(price), is_promo: promo, valid_from: null, promo_until: promo ? (until ?? null) : null,
    club: null, note: promo ? (note ?? null) : null, ref: "", ean: ean ? String(ean) : null };
}
const extra = (o: Offer, price: number, ref: string, club: string | null, note: string | null, until: string | null = null): Offer =>
  ({ ...o, price_incl: Number(price), ref, club, note, is_promo: true, promo_until: until });

/** Pack size from a title: a piece count wins ("72 Plenek"), then "N x size", then weight/volume;
 *  ranges like "2kg-5kg" (diaper sizes) are skipped. */
export function packSize(title: string): string {
  const END = "(?![\\p{L}\\p{N}_])";
  const t = (title ?? "").replace(/\d+(?:[.,]\d+)?\s*(?:kg|g)?\s*[-–]\s*\d+(?:[.,]\d+)?\s*(?:kg|g)(?![\p{L}\p{N}_])/giu, " ");
  for (const pat of [`(\\d+)\\s*(?:ks|kusů|plenek|rolí|role)${END}`, `(\\d+\\s*x\\s*\\d+(?:[.,]\\d+)?\\s*(?:kg|g|ml|l))${END}`,
    `(\\d+(?:[.,]\\d+)?\\s*(?:kg|g|ml|l))${END}`]) {
    const m = t.match(new RegExp(pat, "iu"));
    if (m) return m[0].trim();
  }
  return "";
}
const cand = (o: Offer, offers: Offer[] = [o]): Candidate => ({ ref: o.store_ref, name: o.name, url: o.url, offers });

// ---------------------------------------------------------------- Rohlík
const Rohlik: Source = {
  async search(q, n = 12) {
    const ids: number[] = ((await getJson(`https://www.rohlik.cz/services/frontend-service/autocomplete?${qs([["search", q], ["referer", "whisperer"], ["companyId", 1], ["limit", n]])}`)).productIds ?? []).slice(0, n);
    return (await rohlikCards(ids.map(String))).map((o) => cand(o));
  },
  async fetch(refs) { return Object.fromEntries((await rohlikCards(refs)).map((o) => [o.store_ref, [o]])); },
  refOf: same,
};
async function rohlikCards(ids: string[]): Promise<Offer[]> {
  const out: Offer[] = [];
  for (let i = 0; i < ids.length; i += 20) {
    const cards = await getJson(`https://www.rohlik.cz/api/v1/products/card?${qs([...ids.slice(i, i + 20).map((x) => ["products", x] as [string, string]), ["categoryType", "normal"]])}`);
    for (const c of cards) {
      const p = c.prices ?? {}, sale = p.salePrice, orig = p.originalPrice, cur = sale ?? orig;
      if (cur == null) continue;
      const promo = sale != null && orig != null && sale < orig;
      out.push({ store_id: "rohlik", store_name: "Rohlík", store_ref: String(c.productId), name: c.name ?? "",
        amount: c.textualAmount ?? "", url: `https://www.rohlik.cz/${c.productId}`, price_incl: Number(cur), is_promo: promo,
        valid_from: null, promo_until: promo && p.saleValidTill ? String(p.saleValidTill).slice(0, 10) : null,
        club: null, note: null, ref: "" });
    }
  }
  return out;
}

// ---------------------------------------------------------------- Globus (Shopsys GraphQL)
const GLOBUS_FIELDS = "uuid name catalogNumber ean sellUnitSizeText slug price{priceWithVat globusOriginalPrice globusDiscountPercentage priority}";
async function globusQ(query: string, variables: unknown) {
  const d = await getJson("https://globusonline.cz/graphql/", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables }) });
  if (d.errors?.length) throw new Error(d.errors[0].message);
  return d.data;
}
// deno-lint-ignore no-explicit-any
function globusOffer(n: any): Offer {
  const p = n.price ?? {}, orig = p.globusOriginalPrice, disc = p.globusDiscountPercentage;
  return storeOffer("globus", "Globus", n.catalogNumber, n.name, n.sellUnitSizeText,
    n.slug ? `https://globusonline.cz/${String(n.slug).replace(/^\//, "")}` : "https://globusonline.cz",
    Number(p.priceWithVat), orig ? Number(orig) : null, null, disc ? `−${Math.round(Number(disc))} %` : null, n.ean);
}
const Globus: Source = {
  eanSearch: true,
  async search(q, n = 12) {
    const d = await globusQ(`query S($q:String!,$n:Int!){productsSearch(searchInput:{search:$q,isAutocomplete:false,userIdentifier:"00000000-0000-4000-8000-000000000000"},first:$n){edges{node{${GLOBUS_FIELDS}}}}}`, { q, n });
    // deno-lint-ignore no-explicit-any
    let nodes = d.productsSearch.edges.map((e: any) => e.node);
    // deno-lint-ignore no-explicit-any
    if (isEan(q)) nodes = nodes.filter((x: any) => x.ean === q.trim());  // EAN search is fuzzy
    return nodes.map((x: unknown) => cand(globusOffer(x)));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (let i = 0; i < refs.length; i += 50) {
      const d = await globusQ(`query($c:[String!]!){productsByCatnums(catnums:$c){${GLOBUS_FIELDS}}}`, { c: refs.slice(i, i + 50) });
      for (const x of d.productsByCatnums ?? []) out[x.catalogNumber] = [globusOffer(x)];
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- Košík.cz
// deno-lint-ignore no-explicit-any
function kosikOffer(it: any): Offer {
  const q = it.productQuantity ?? {};
  return storeOffer("kosik-cz", "Košík.cz", it.id, it.name, q.value ? `${+q.value} ${q.unit}` : "",
    "https://www.kosik.cz" + (it.url ?? `/p${it.id}`), it.price, it.recommendedPrice, kupiDates(it.actionLabel ?? "")[1],
    it.percentageDiscount ? `−${it.percentageDiscount} %` : null);
}
const Kosik: Source = {
  eanSearch: true,
  async search(q, n = 12) {
    const d = await getJson(`https://www.kosik.cz/api/front/suggest/v2?${qs([["query", q], ["limit", n]])}`);
    return (d.products?.items ?? []).map((it: unknown) => cand(kosikOffer(it)));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (const ref of refs) {
      try { out[ref] = [kosikOffer((await getJson(`https://www.kosik.cz/api/front/product/${ref}`)).product)]; }
      catch (e) { console.error(`kosik ${ref}: ${e}`); }
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- Tesco (xapi GraphQL; key in env)
const TESCO_NODE = "id gtin title isForSale price{actual unitPrice unitOfMeasure} promotions{startDate endDate description attributes price{beforeDiscount afterDiscount}}";
async function tescoPost(ops: unknown[]) {
  const key = env("TESCO_XAPI_KEY");
  if (!key) throw new Error("TESCO_XAPI_KEY missing");
  return await getJson("https://xapi.tesco.com/", { method: "POST", body: JSON.stringify(ops),
    headers: { "Content-Type": "application/json", "x-apikey": key, language: "cs-CZ", region: "CZ" } });
}
// deno-lint-ignore no-explicit-any
function tescoOffers(n: any): Offer[] {
  if (!n?.price || n.price.actual == null) return [];
  let regular: number | null = null, until: string | null = null, club: [number, string | null] | null = null;
  for (const pr of n.promotions ?? []) {
    if ((pr.attributes ?? []).includes("CLUBCARD_PRICING")) {
      const m = (pr.description ?? "").match(/(\d+(?:[  ]\d{3})*,\d{2})\s*Kč/);
      if (m) club = [Number(m[1].replace(/[  ]/g, "").replace(",", ".")), (pr.endDate ?? "").slice(0, 10) || null];
    } else if (pr.price?.beforeDiscount) { regular = pr.price.beforeDiscount; until = (pr.endDate ?? "").slice(0, 10) || null; }
  }
  const o = storeOffer("tesco", "Tesco", n.id, n.title, packSize(n.title), `https://nakup.itesco.cz/groceries/cs-CZ/products/${n.id}`,
    n.price.actual, regular, until, null, (n.gtin ?? "").replace(/^0+/, "") || null);
  return club && club[0] < o.price_incl ? [o, extra(o, club[0], "clubcard", "Tesco Clubcard", null, club[1])] : [o];
}
const Tesco: Source = {
  eanViaName: true,  // no EAN search, but every product carries its gtin
  async search(q, n = 12) {
    const d = await tescoPost([{ operationName: "S", variables: { q, n },
      query: `query S($q:String!,$n:Int){search(query:$q,page:1,count:$n){results{node{... on ProductInterface{${TESCO_NODE}}}}}}` }]);
    // deno-lint-ignore no-explicit-any
    return ((d[0]?.data?.search?.results) ?? []).map((r: any) => tescoOffers(r.node)).filter((x: Offer[]) => x.length).map((x: Offer[]) => cand(x[0], x));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (let i = 0; i < refs.length; i += 20) {
      const res = await tescoPost(refs.slice(i, i + 20).map((t, j) => ({ operationName: `P${j}`, variables: { t },
        query: `query P${j}($t:String){product(tpnc:$t){${TESCO_NODE}}}` })));
      for (const r of res) { const o = tescoOffers(r?.data?.product); if (o.length) out[o[0].store_ref] = o; }
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- Billa (catalogue API, haléře)
// deno-lint-ignore no-explicit-any
function billaOffer(p: any): Offer | null {
  const pr = p.price ?? {}, cur = pr.regular?.value;
  if (cur == null) return null;
  const std = pr.standard?.value ?? pr.crossed;
  return storeOffer("billa", "Billa", p.sku, p.name, `${p.amount ?? ""} ${p.volumeLabelShort ?? ""}`.trim(),
    `https://www.billa.cz/produkt/${p.slug ?? p.sku}`, cur / 100, std ? std / 100 : null, null,
    pr.discountPercentage ? `${pr.discountPercentage} %` : null);
}
const Billa: Source = {
  async search(q, n = 12) {
    const d = await getJson(`https://www.billa.cz/api/product-discovery/products/search/${encodeURIComponent(q)}?pageSize=${n}`);
    return (d.results ?? []).map(billaOffer).filter(Boolean).map((o: Offer) => cand(o));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (let i = 0; i < refs.length; i += 30) {
      const d = await getJson(`https://www.billa.cz/api/product-discovery/products?${qs([...refs.slice(i, i + 30).map((r) => ["skus", r] as [string, string]), ["pageSize", 30]])}`);
      for (const p of d.results ?? []) { const o = billaOffer(p); if (o) out[o.store_ref] = [o]; }
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- dm
const czk = (s?: string) => { const m = (s ?? "").replace(/ /g, " ").match(/[\d ]+,\d+|\d+/); return m ? Number(m[0].replace(/ /g, "").replace(",", ".")) : null; };
// deno-lint-ignore no-explicit-any
function dmOffer(p: any): Offer | null {
  const pr = (p.tileData ?? p).price ?? {};
  let cur = czk(pr.price?.current?.value);
  const prev = czk(pr.price?.previous?.value);
  if (cur == null) cur = (p.trackingData ?? p.tileData?.trackingData ?? {}).price ?? null;
  if (cur == null) return null;
  const infos = pr.tileInfos ?? pr.infos ?? [];
  const title = typeof p.title === "object" ? p.title?.tileHeadline : p.title;
  return storeOffer("dm", "dm", p.dan, `${p.brandName ?? ""} ${title ?? ""}`.trim(), infos[0] ? infos[0].split("(")[0].trim() : "",
    "https://www.dm.cz" + (p.tileData?.self ?? p.self ?? `/p/d/${p.dan}`), cur, prev, null, null, p.gtin);
}
const Dm: Source = {
  eanSearch: true,
  async search(q, n = 10) {
    const r = await fetch(`https://product-search.services.dmtech.com/cz/search/crawl?${qs([["query", q], ["searchType", "product"], ["pageSize", n]])}`,
      { headers: { "User-Agent": UA }, signal: AbortSignal.timeout(20000) });
    if (r.status === 429) throw new Error("dm is rate-limiting searches, try again in ~20 s");
    if (!r.ok) throw new Error(`dm HTTP ${r.status}`);
    // deno-lint-ignore no-explicit-any
    let prods: any[] = (await r.json()).products ?? [];
    if (isEan(q)) { const exact = prods.filter((p) => String(p.gtin) === q.trim()); if (exact.length) prods = exact; }
    return prods.map(dmOffer).filter(Boolean).map((o) => cand(o as Offer));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (let i = 0; i < refs.length; i += 30) {
      const d = await getJson(`https://products.dm.de/product/products/tiles/CZ/dans/${refs.slice(i, i + 30).join(",")}`);
      for (const [dan, p] of Object.entries(d.products ?? {})) { const o = dmOffer({ ...(p as object), dan: (p as { dan?: unknown }).dan ?? dan }); if (o) out[String(dan)] = [o]; }
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- Rossmann + Teta (Luigi's Box)
async function luigi(tracker: string, q: string, size = 12, filters: string[] = []) {
  const d = await getJson(`https://live.luigisbox.com/search?${qs([["tracker_id", tracker], ["q", q], ["size", size], ...filters.map((f) => ["f[]", f] as [string, string])])}`);
  return d.results?.hits ?? [];
}
// deno-lint-ignore no-explicit-any
function rossmannOffers(h: any): Offer[] {
  const a = h.attributes ?? {}, reg = a.price_regular_CZK_incVat, ids = a.id_erp ?? [];
  if (reg == null || !ids.length) return [];
  const eans: string[] = (a.id_ean ?? []).map((e: string) => e.replace(/^0+/, ""));
  const o = storeOffer("rossmann", "Rossmann", ids[0], `${(a.PRODUCER ?? [""])[0]} ${a.title ?? ""}`.trim(), packSize(a.title ?? ""),
    `https://www.rossmann.cz/vyhledavani?q=${ids[0]}`, reg, null, null, null, eans[0] ?? null);
  o.eans = eans;
  const out = [o];
  for (const [key, label] of [["price_clubMember_CZK_incVat", "Rossmann Club"], ["price_rossmanekMember_CZK_incVat", "Rossmánek (kids club)"]]) {
    const v = a[key];
    if (v != null && v < reg - 0.001) out.push(extra(o, v, key.split("_")[1], label, null));
  }
  return out;
}
const Rossmann: Source = {
  eanSearch: true,
  async search(q, n = 12) {
    let c = (await luigi("715445-966389", q, n)).map(rossmannOffers).filter((x: Offer[]) => x.length);
    if (isEan(q)) c = c.filter((x: Offer[]) => (x[0].eans ?? []).includes(q.trim().replace(/^0+/, "")));  // unknown EANs get fuzzy junk
    return c.map((x: Offer[]) => cand(x[0], x));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (const ref of refs) {
      for (const offs of (await luigi("715445-966389", ref, 3)).map(rossmannOffers)) {
        if (offs.length && offs[0].store_ref === ref) { out[ref] = offs; break; }
      }
    }
    return out;
  },
  refOf: same,
};

// deno-lint-ignore no-explicit-any
function tetaOffers(code: string, name: string, prices: any, ean: string | null, amount: string): Offer[] {
  if (typeof prices === "string") prices = JSON.parse(prices || "{}");
  const zc = prices?.zcmd;
  if (zc == null) return [];
  const o = storeOffer("teta", "Teta", code, name, amount, `https://www.tetadrogerie.cz/vyhledavani?q=${code}`, zc / 100, null, null, null, ean);
  const out = [o];
  if (prices.acmd != null && prices.acmd < zc) out.push(extra(o, prices.acmd / 100, "akce", null, prices.conditions || null));
  if (prices.club != null && prices.club < zc) out.push(extra(o, prices.club / 100, "club", "Teta club", null));
  return out;
}
const Teta: Source = {
  eanSearch: true,
  async search(q, n = 12) {
    const out: Candidate[] = [];
    for (const h of await luigi("407635-780988", q, n, ["type:product_variant"])) {
      const a = h.attributes ?? {}, code = (a.code ?? [])[0], eans: string[] = a.ean ?? [];
      if (!code || (isEan(q) && !eans.includes(q.trim()))) continue;
      const offs = tetaOffers(code, a.title ?? "", a.prices ?? "{}", eans[0] ?? null, packSize(a.title ?? ""));
      if (offs.length) out.push(cand(offs[0], offs));
    }
    return out;
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (const code of refs) {
      try {
        const d = await getJson(`https://be.tetadrogerie.cz/api/v2/shop/product-variants/${code.padStart(18, "0")}`, { headers: { Accept: "application/ld+json" } });
        const offs = tetaOffers(code, d.name ?? "", d.bbyPrices ?? {}, null, packSize(d.name ?? ""));
        if (offs.length) out[code] = offs;
      } catch (e) { console.error(`teta ${code}: ${e}`); }
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- Makro (net prices; VAT added per item)
const MAKRO = "https://sortiment.makro.cz", MAKRO_STORE = () => env("MAKRO_STORE") ?? "00005";
const makroSearch = (q: string, rows = 12) => getJson(`${MAKRO}/searchdiscover/articlesearch/search?${qs([["storeId", MAKRO_STORE()], ["language", "cs-CZ"], ["country", "CZ"], ["query", q], ["rows", rows], ["page", 1]])}`);
async function makroBundles(ids: string[], prices: Record<string, number | null>) {
  // deno-lint-ignore no-explicit-any
  const arts: any = {};
  for (let i = 0; i < ids.length; i += 20) {  // long id lists -> HTTP 400
    const d = await getJson(`${MAKRO}/evaluate.article.v1/betty-variants?${qs([["storeIds", MAKRO_STORE()], ["country", "CZ"], ["locale", "cs-CZ"], ...ids.slice(i, i + 20).map((x) => ["ids", x] as [string, string])])}`);
    Object.assign(arts, d.result ?? {});
  }
  const out: { ean: string; desc: string; food: boolean; net: number | null }[] = [];
  // deno-lint-ignore no-explicit-any
  for (const art of Object.values(arts) as any[]) {
    // deno-lint-ignore no-explicit-any
    for (const v of Object.values(art.variants ?? {}) as any[]) {
      const vid = v.bettyVariantId?.bettyVariantId;
      // deno-lint-ignore no-explicit-any
      const bundles = Object.values(v.bundles ?? {}) as any[];
      const smallest = Math.min(...bundles.map((b) => Number(b.bundleSize ?? 1)), Infinity);
      for (const b of bundles) {
        // deno-lint-ignore no-explicit-any
        const eans = (b.gtins ?? []).map((g: any) => g.number).filter(Boolean);
        if (!eans.length) continue;
        out.push({ ean: eans[0], desc: b.description ?? "", food: !!art.food, net: Number(b.bundleSize ?? 1) === smallest ? (prices[vid] ?? null) : null });
      }
    }
  }
  return out;
}
const makroOffer = (ean: string, desc: string, net: number, food: boolean): Offer => ({
  store_id: "makro", store_name: "Makro", store_ref: ean, name: desc, amount: "", url: `${MAKRO}/shop/search?q=${ean}`,
  price_incl: Math.round(net * (food ? 1.12 : 1.21) * 100) / 100, price_net: net, is_promo: false, valid_from: null,
  promo_until: null, club: null, note: null, ref: "" });
const Makro: Source = {
  eanSearch: true,
  async search(q, n = 12) {
    const res = (await makroSearch(q, n)).results ?? {};
    const prices = Object.fromEntries(Object.entries(res).map(([k, v]) => [k, (v as { price?: number }).price ?? null]));
    return (await makroBundles(Object.keys(res).slice(0, n), prices)).map(({ ean, desc, food, net }) => ({
      ref: ean, name: desc, url: `${MAKRO}/shop/search?q=${ean}`,
      // the VAT class isn't knowable here ('food' covers diapers): candidates show the net price
      offers: net != null ? [{ ...makroOffer(ean, desc, net, food), price_incl: net, amount: "bez DPH" }] : [] }));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (const ean of refs) {
      const res = (await makroSearch(ean, 3)).results ?? {};
      const [vid, hit] = Object.entries(res)[0] ?? [];
      if (!vid) continue;
      const b = (await makroBundles([vid], { [vid]: (hit as { price?: number }).price ?? null })).find((x) => x.ean === ean);
      if (b && b.net != null) out[ean] = [makroOffer(ean, b.desc, b.net, b.food)];
    }
    return out;
  },
  refOf: same,
};

// ---------------------------------------------------------------- Kupi (flyer aggregator, HTML)
const KUPI = "https://www.kupi.cz";
const CHAINS: Record<string, string> = { "penny-market": "penny", "teta-drogerie": "teta", "dm-drogerie": "dm" };
const ENT: Record<string, string> = { nbsp: " ", amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", ndash: "–", mdash: "—" };
const text = (s: string) => s.replace(/<svg[\s\S]*?<\/svg>|<[^>]+>/g, " ")
  .replace(/&(#x?[0-9a-f]+|\w+);/gi, (m, e) => e[0] === "#" ? String.fromCodePoint(parseInt(e.slice(1).replace(/^x/i, ""), e[1]?.toLowerCase() === "x" ? 16 : 10)) : ENT[e.toLowerCase()] ?? m)
  .replace(/\s+/g, " ").trim();

/** "platí do úterý 29. 9." | "čt 24. 9. – ne 27. 9." | "dnes končí" | "zítra končí" -> [from, to] (Prague dates). */
export function kupiDates(t: string): [string | null, string | null] {
  const now = new Date(new Date().toLocaleString("sv-SE", { timeZone: "Europe/Prague" }).replace(" ", "T") + "Z");
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  if (t.includes("dnes končí")) return [null, iso(now)];
  if (t.includes("zítra končí")) return [null, iso(new Date(now.getTime() + 86400000))];
  const ds = [...t.matchAll(/(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?/g)].map(([, d, m, y]) => {
    const day = new Date(Date.UTC(y ? +y : now.getUTCFullYear(), +m - 1, +d));
    if (!y && (now.getTime() - day.getTime()) / 86400000 > 180) day.setUTCFullYear(day.getUTCFullYear() + 1);
    return iso(day);
  });
  if (ds.length >= 2) return [ds[0], ds[ds.length - 1]];
  if (ds.length === 1) return /(^|[^\p{L}])od([^\p{L}]|$)/u.test(t) ? [ds[0], null] : [null, ds[0]];
  return [null, null];
}

/** All offers on a Kupi page, deduped by offer id: product number -> offers. */
function kupiRows(page: string): Map<string, Offer[]> {
  const out = new Map<string, Offer[]>(), seen = new Set<string>();
  for (const chunk of page.split('<div class="discount_row').slice(1)) {
    const did = chunk.match(/data-discount="(\d+)"/)?.[1];
    if (!did || seen.has(did)) continue;
    seen.add(did);
    const pid = chunk.match(/data-product="(\d+)"/)?.[1];
    const shop = chunk.match(/discounts_shop_name">\s*<a href="\/letaky\/([^"/?]+)"[^>]*title="([^"]*)"/);
    const price = chunk.match(/discount_price_value">([^<]+)</)?.[1];
    if (!pid || !shop || !price) continue;
    const amount = chunk.match(/discount_amount">([\s\S]*?)<\/div>/)?.[1];
    const valid = chunk.match(/discounts_validity[^"]*">([\s\S]*?)<\/div>\s*<\/div>/)?.[1];
    const club = chunk.match(/discounts_club">([\s\S]*?)<\/div>/)?.[1];
    const note = chunk.match(/discount_note">([\s\S]*?)<\/div>/)?.[1];
    const [vf, vt] = kupiDates(valid ? text(valid) : "");
    const list = out.get(pid) ?? (out.set(pid, []), out.get(pid)!);
    list.push({ store_id: CHAINS[shop[1]] ?? shop[1], store_name: text(shop[2]), store_ref: "",
      key: chunk.match(/data-key="([^"]*)"/)?.[1] ?? "vse", name: "",
      amount: amount ? text(amount).replace(/^[/ ]+/, "").trim() : "", url: "",
      price_incl: Number(text(price).replace("Kč", "").replace(/ /g, "").replace(",", ".")),
      is_promo: true, valid_from: vf, promo_until: vt, club: club ? text(club) || null : null,
      note: note ? text(note) || null : null, ref: did });
  }
  return out;
}
function kupiProducts(page: string): Map<string, [string, string]> {
  const found = new Map<string, [string, string]>();
  for (const m of page.matchAll(/<a href="\/sleva\/([^"]+)" title="([^"]*)"[^>]*data-product-id="(\d+)"/g)) {
    if (!found.has(m[3])) found.set(m[3], [m[1], text(m[2])]);
  }
  return found;
}
const kupiFinish = (slug: string, name: string, offers: Offer[]) =>
  offers.map((o) => ({ ...o, store_ref: `kupi:${slug}:${o.key}`, name: `${name} ${o.amount}`.trim(), product: name, slug, url: `${KUPI}/sleva/${slug}` }));

export const Kupi: Source & { crawlPage(chain: string, page: number): Promise<{ offers: Offer[]; products: number }> } = {
  aggregator: true,
  async search(q) {
    const page = await getText(`${KUPI}/hledej?${qs([["f", q]])}`);
    const rows = kupiRows(page);
    return [...kupiProducts(page)].map(([pid, [slug, name]]) => ({ ref: slug, name, url: `${KUPI}/sleva/${slug}`, offers: kupiFinish(slug, name, rows.get(pid) ?? []) }));
  },
  async fetch(refs) {
    const out: Record<string, Offer[]> = {};
    for (const slug of refs) {
      const page = await getText(`${KUPI}/sleva/${slug}`);
      const name = page.match(/"@type":"Product","name":"([^"]+)"/)?.[1] ?? slug;
      out[slug] = kupiFinish(slug, name, [...kupiRows(page).values()].flat());
    }
    return out;
  },
  /** One page of a chain's current flyer (for the crawl job). */
  async crawlPage(chain, page) {
    const html = await getText(`${KUPI}/slevy/${chain}${page > 1 ? `?page=${page}` : ""}`);
    const rows = kupiRows(html), prods = kupiProducts(html);
    return { products: prods.size, offers: [...prods].flatMap(([pid, [slug, name]]) => kupiFinish(slug, name, rows.get(pid) ?? [])) };
  },
  refOf: (sr) => sr.split(":")[1],
};

export const SOURCES: Record<string, Source> = { globus: Globus, kosik: Kosik, tesco: Tesco, billa: Billa, dm: Dm,
  rossmann: Rossmann, teta: Teta, makro: Makro, rohlik: Rohlik, kupi: Kupi };
export function source(name: string): Source {
  const s = SOURCES[name];
  if (!s) throw new Error(`no source ${name}`);
  return s;
}
export { inferUnit };
