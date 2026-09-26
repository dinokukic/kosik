// Read side of the API: matrix, products (match screen), shopping list, saved lists.
// Same JSON shapes as pricetrack.py's local server, so the dashboard pages work unchanged.
// The heavy lifting (current price, per-unit, cheapest per row, best flyer deal) lives in
// SQL views in Bunny Database (v_matrix, v_row_deal, v_offer, v_deal).
import { readMany, Row } from "./db.ts";

const KUPI_BASE = "https://www.kupi.cz";
const AGGREGATORS = new Set(["kupi"]);
/** A watch's ref from an offering's store_ref (Kupi: "kupi:<slug>:<pack>" -> slug). */
const refOf = (source: string, storeRef: string) => source === "kupi" ? storeRef.split(":")[1] : storeRef;
const byStr = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);  // Python's sort order, not locale

const CELL_FIELDS = ["offering_id", "offering_name", "source", "price_incl", "unit_qty", "unit_price",
  "is_promo", "promo_until", "club", "note", "ts", "url"];
const DEAL_FIELDS = ["store_id", "store_name", "product_name", "amount", "price_incl", "unit_price",
  "valid_from", "valid_to", "club", "note", "slug"];
const pick = (o: Row, keys: string[]) => Object.fromEntries(keys.map((k) => [k, o[k] ?? null]));

export type MatrixRow = { key: string; name: string; category: string | null; unit: string; cells: Record<string, Row>; deal: Row | null };

/** Store cells from direct sources + each row's best Kupi flyer deal; the cheapest mark goes
 *  to whichever is lower per unit (a store cell or the flyer deal). */
export async function matrixRows(): Promise<{ stores: Row[]; rows: MatrixRow[] }> {
  const [cells, deals, stores] = await readMany(
    ["SELECT * FROM v_matrix ORDER BY category, row_name"],
    ["SELECT * FROM v_row_deal"],
    ["SELECT id, name FROM store WHERE id IN (SELECT DISTINCT store_id FROM v_matrix) ORDER BY id"],
  );
  const rows = new Map<string, MatrixRow>();
  const row = (key: string, name: string, category: string | null, unit: string) => {
    if (!rows.has(key)) rows.set(key, { key, name, category, unit, cells: {}, deal: null });
    return rows.get(key)!;
  };
  for (const c of cells) {
    row(c.row_key, c.row_name, c.category, c.unit).cells[c.store_id] = { ...pick(c, CELL_FIELDS), is_cheapest: !!c.is_cheapest };
  }
  for (const d of deals) {
    const r = row(d.row_key, d.row_name, d.category, d.row_unit);
    r.deal = { ...pick(d, DEAL_FIELDS), url: `${KUPI_BASE}/sleva/${d.slug}`, is_cheapest: false };
    const best = Math.min(...Object.values(r.cells).map((x) => x.unit_price), Infinity);
    if (d.unit_price <= best) {
      r.deal.is_cheapest = true;
      if (d.unit_price < best) for (const x of Object.values(r.cells)) x.is_cheapest = false;
    }
  }
  const sorted = [...rows.values()].sort((a, b) => byStr(a.category ?? "", b.category ?? "") || byStr(a.name, b.name));
  return { stores, rows: sorted };
}

/** Snapshot of one matrix cell (what goes on the shopping list), or null if it has no price. */
export function matrixCell(rows: MatrixRow[], stores: Row[], rowKey: string, storeId: string, kind: string) {
  const row = rows.find((r) => r.key === rowKey);
  if (!row) return { row: null, snap: null };
  if (kind === "flyer") {
    const d = row.deal;
    if (!d || d.store_id !== storeId) return { row, snap: null };
    return { row, snap: { store_name: d.store_name || storeId, item_name: d.product_name, amount: d.amount,
      price_incl: d.price_incl, unit_price: d.unit_price, promo_until: d.valid_to, club: d.club, note: d.note, url: d.url } };
  }
  const c = row.cells[storeId];
  if (!c) return { row, snap: null };
  const name = stores.find((s) => s.id === storeId)?.name ?? storeId;
  return { row, snap: { store_name: name, item_name: c.offering_name, amount: null, price_incl: c.price_incl,
    unit_price: c.unit_price, promo_until: c.promo_until, club: c.club, note: c.note, url: c.url } };
}

export async function matrix() {
  const [{ stores, rows }, [cart]] = await Promise.all([
    matrixRows(), readMany(["SELECT row_key, store_id, kind FROM cart_item"])]);
  return {
    generated: new Date().toISOString().slice(0, 16),
    stores, rows,
    cart: Object.fromEntries(cart.map((c) => [c.row_key, { store_id: c.store_id, kind: c.kind }])),
  };
}

/** The shopping list, each line with the current price at the same store (null if gone). */
export async function cartItems() {
  const [{ stores, rows }, [items]] = await Promise.all([
    matrixRows(), readMany(["SELECT * FROM cart_item ORDER BY store_name, row_name"])]);
  return items.map((it) => ({ ...it, current_price: matrixCell(rows, stores, it.row_key, it.store_id, it.kind).snap?.price_incl ?? null }));
}

export async function savedLists() {
  const [lists] = await readMany(["SELECT * FROM saved_list ORDER BY created DESC"]);
  return lists.map((l) => ({ id: l.id, name: l.name, created: l.created, total: l.total, count: JSON.parse(l.items).length }));
}
export async function savedList(id: number) {
  const [[l]] = await readMany(["SELECT * FROM saved_list WHERE id=?", [id]]);
  if (!l) throw new HttpError(404, "no such list");
  return { id: l.id, name: l.name, created: l.created, total: l.total, items: JSON.parse(l.items) };
}

/** Receipt name -> search text the stores understand (port of pricetrack.default_query). */
export function defaultQuery(name: string): string {
  let q = name.replace(/\b(MC|FL|ASC|NN|PAC)\b/g, " ").replace(/\bTP\b/g, "toaletní papír");
  q = q.replace(/\d+([.,]\d+)?\s*x\s*\d+\s*g\b|\d+([.,]\d+)?\s*(kg|g|ml|l|L|ks)\b|\d+-vrstv\p{L}*|\d+\/\d+|\d+\s*%|\bvel\.\s*\d+|\bvaničk\p{L}*/gu, " ");
  return q.replace(/\s+/g, " ").replace(/^[\s.,]+|[\s.,]+$/g, "").toLowerCase();
}
export const searchText = (p: Row) => defaultQuery(p.search_query || p.name);

/** Every product with what it's matched to per source and today's offers (match screen). */
export async function products() {
  const [prods, watches, offers, stores, cands, deals] = await readMany(
    [`SELECT p.*, n.name AS need_name FROM product p LEFT JOIN need n ON n.id = p.need_id ORDER BY p.category, p.name`],
    ["SELECT * FROM watch WHERE product_id IS NOT NULL"],
    [`SELECT product_id, source, store_ref, store_id, offering_name, price_incl, unit_price, unit, promo_until, club
      FROM v_offer WHERE product_id IS NOT NULL`],
    ["SELECT id, name FROM store"],
    ["SELECT * FROM ean_candidate ORDER BY score DESC"],
    [`SELECT product_id, slug, store_id, store_name, amount, price_incl, valid_from, valid_to AS promo_until, club, note
      FROM v_deal WHERE product_id IS NOT NULL ORDER BY price_incl`],
  );
  const storeName = Object.fromEntries(stores.map((s) => [s.id, s.name]));
  const group = <T extends Row>(xs: T[], key: string) => {
    const m = new Map<unknown, T[]>();
    for (const x of xs) (m.get(x[key]) ?? m.set(x[key], []).get(x[key])!).push(x);
    return m;
  };
  const wBy = group(watches, "product_id"), oBy = group(offers, "product_id"),
    cBy = group(cands, "product_id"), dBy = group(deals, "product_id");
  for (const p of prods) {
    const mine = oBy.get(p.id) ?? [];
    p.query = searchText(p);
    p.ean_candidates = p.ean ? [] : (cBy.get(p.id) ?? []);
    p.matches = (wBy.get(p.id) ?? []).map((w) => AGGREGATORS.has(w.source)
      ? { source: w.source, ref: w.ref, name: w.name || w.ref, packs: w.packs,
          offers: (dBy.get(p.id) ?? []).filter((d) => d.slug === w.ref) }
      : { source: w.source, ref: w.ref, name: w.name || w.ref,
          offers: mine.filter((o) => o.source === w.source && refOf(o.source, o.store_ref) === w.ref && o.price_incl !== null)
            .map((o) => ({ ...o, store_name: storeName[o.store_id] ?? o.store_id })) });
    p.manual = mine.filter((o) => o.source === "manual" && o.price_incl !== null)
      .map((o) => ({ ...o, store_name: storeName[o.store_id] ?? o.store_id }));
  }
  return prods;
}

export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
