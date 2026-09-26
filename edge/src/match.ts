// Matching on the edge: search a store, link a picked product (cache the store's id + save its
// current offers), barcode matches across stores, Makro lookup for new items.
// A request may make at most 50 subrequests (each DB call is one), so a link = the store call(s)
// + one read + ONE batched write; prices find their offering via a subquery, not a read-back.
import { all, db, first, Row } from "./db.ts";
import { HttpError, searchText } from "./read.ts";
import { Candidate, Offer, source, SOURCES } from "./stores.ts";
import { inferUnit, today } from "./write.ts";

const stmt = (sql: string, ...args: unknown[]) => db().prepare(sql).bind(...(args.map((a) => a === undefined ? null : a) as never[]));

export async function search(src: string, q: string, n = 60): Promise<Candidate[]> {
  return await source(src).search(q, Math.max(1, Math.min(n, 150)));
}

/** Statements that create/refresh an offering per (store, store_ref) and record each offer as a
 *  price row (port of pricetrack.save_offers). A live price retires the hand-entered one. */
export function offerStatements(src: string, offers: Offer[], product: Row, qty: number | null = null) {
  const out = [];
  const ts = today();
  for (let o of offers) {
    if (o.price_net != null && product.vat_pct) o = { ...o, price_incl: Math.round(o.price_net * (1 + product.vat_pct / 100) * 100) / 100 };
    const [unit, guess] = inferUnit(o.size || `${o.name} ${o.amount}`);
    let q: number | null = qty ?? (unit === product.unit ? guess : null);
    if (q != null && Math.abs(q - product.unit_qty) < 1e-9) q = null;  // same pack as the item: inherit
    out.push(
      stmt("INSERT OR IGNORE INTO store(id,name) VALUES (?,?)", o.store_id, o.store_name || o.store_id),
      stmt(`INSERT INTO offering(store_id,store_ref,source,name,product_id,need_id,url,unit_qty) VALUES (?,?,?,?,?,NULL,?,?)
            ON CONFLICT(store_id,store_ref) DO UPDATE SET name=excluded.name, url=excluded.url, source=excluded.source, active=1,
              product_id=COALESCE(excluded.product_id, offering.product_id), unit_qty=excluded.unit_qty`,
        o.store_id, o.store_ref, src, o.name, product.id, o.url, q),
      stmt(`INSERT OR REPLACE INTO price(offering_id,ts,ref,price_incl,is_promo,valid_from,promo_until,club,note)
            VALUES ((SELECT id FROM offering WHERE store_id=? AND store_ref=?),?,?,?,?,?,?,?,?)`,
        o.store_id, o.store_ref, ts, o.ref ?? "", Math.round(o.price_incl * 100) / 100, o.is_promo ? 1 : 0,
        o.valid_from ?? ts, o.promo_until, o.club, o.note),
      stmt("UPDATE offering SET active=0 WHERE source='manual' AND store_id=? AND product_id=?", o.store_id, product.id),
    );
  }
  return out;
}

/** Kupi flyer offers -> the deal table (port of pricetrack.save_deals). */
export function dealStatements(offers: Offer[]) {
  const ts = today(), out = [];
  for (const [id, name] of new Map(offers.map((o) => [o.store_id, o.store_name]))) out.push(stmt("INSERT OR IGNORE INTO store(id,name) VALUES (?,?)", id, name));
  for (const o of offers) {
    const [unit, qty] = inferUnit(`${o.product ?? ""} ${o.amount}`);
    out.push(stmt(`INSERT INTO deal(ref,slug,product_name,store_id,store_name,pack_key,amount,unit,unit_qty,price_incl,valid_from,valid_to,club,note,first_seen,last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ref) DO UPDATE SET price_incl=excluded.price_incl, valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                     club=excluded.club, note=excluded.note, amount=excluded.amount, unit=excluded.unit, unit_qty=excluded.unit_qty,
                     last_seen=excluded.last_seen`,
      o.ref, o.slug, o.product, o.store_id, o.store_name, o.key, o.amount, unit, unit !== "pack" ? qty : null,
      o.price_incl, o.valid_from, o.promo_until, o.club, o.note, ts, ts));
  }
  return out;
}

/** Cache a picked candidate as a watch and pull its current offers: store offerings for a
 *  store, flyer deals for Kupi. Returns how many offers came back (0 is fine for Kupi: the
 *  product stays watched and later crawls pick it up). */
export async function link(src: string, ref: string, productId: number, name?: string, packs?: string[] | string | null, qty?: number | null) {
  const product = await first("SELECT * FROM product WHERE id=?", productId);
  if (!product) throw new HttpError(404, `no product ${productId}`);
  const s = source(src);
  const packStr = Array.isArray(packs) ? packs.join(",") : (packs || null);
  const offers = (await s.fetch([ref]))[ref] ?? [];
  const batch = [stmt(`INSERT INTO watch(source,ref,name,product_id,need_id,qty,packs) VALUES (?,?,?,?,NULL,?,?)
                       ON CONFLICT DO UPDATE SET name=excluded.name, qty=excluded.qty, packs=excluded.packs`,
    src, ref, name ?? null, productId, qty ?? null, packStr)];
  if (s.aggregator) {
    batch.push(...dealStatements(offers));  // the flyer layer; the pack filter applies in v_deal
    await db().batch(batch, { mode: "immediate" });
    const keep = packStr ? new Set(packStr.split(",")) : null;
    return offers.filter((o) => !keep || !o.key || keep.has(o.key)).length;
  }
  // A store listing belongs to one item. Linking it again would silently move it (and a later
  // unlink would delete it from under the other item), so refuse and say where it lives.
  for (const o of offers) {
    const owner = await first(`SELECT p.id, p.name FROM offering o JOIN product p ON p.id = o.product_id
                               WHERE o.store_id=? AND o.store_ref=? AND o.product_id != ?`, o.store_id, o.store_ref, productId);
    if (owner) throw new HttpError(409, `This ${o.store_name} listing already belongs to “${owner.name}”. Unlink it there first, or group both items under one need.`);
  }
  batch.push(...offerStatements(src, offers, product, qty ?? null));
  if (!offers.length) batch.push(stmt("UPDATE offering SET active=0 WHERE source=? AND product_id=? AND store_ref=?", src, productId, ref));
  await db().batch(batch, { mode: "immediate" });
  return offers.length;
}

/** Barcode matches: ask every barcode-aware store for the item's EAN (Tesco: by name, then keep
 *  the exact gtin), in parallel; return hits not linked yet. Košík doesn't echo EANs back, so
 *  its first hit is shown for the user to check. */
export async function suggest(productId: number) {
  const [p, watches] = await Promise.all([first("SELECT * FROM product WHERE id=?", productId),
    all("SELECT source, ref FROM watch WHERE product_id=?", productId)]);
  if (!p?.ean) return [];
  const linked = new Set(watches.map((w) => `${w.source}|${w.ref}`));
  const tasks = Object.entries(SOURCES).filter(([, s]) => s.eanSearch || s.eanViaName).map(async ([name, s]) => {
    try {
      const cands = await s.search(s.eanViaName ? searchText(p) : p.ean, 12);
      const hits = [];
      for (const cd of cands) {
        const eans = new Set<string>([...cd.offers.map((o) => o.ean ?? ""), ...cd.offers.flatMap((o) => o.eans ?? []), ...(name === "makro" ? [cd.ref] : [])]);
        if (name !== "kosik" && !eans.has(p.ean)) continue;  // exact barcode only
        if (linked.has(`${name}|${cd.ref}`)) continue;
        hits.push({ ...cd, source: name });
        if (name === "kosik") break;
      }
      return hits;
    } catch (e) { console.error(`suggest ${name}: ${e}`); return []; }
  });
  return (await Promise.all(tasks)).flat();
}

/** EAN or name -> what Makro calls it (prefills a new item). */
export async function lookup(q: string) {
  return (await source("makro").search(q, 12)).slice(0, 8).map((c) => ({ source: "makro", ean: c.ref, name: c.name }));
}
