// Write side of the API: shopping list, saved lists, items (add / edit / delete), unlinking.
// Ports of the pricetrack.py functions of the same names; multi-step changes are one batch
// (one transaction) in Bunny Database.
import { all, db, first, Row, run } from "./db.ts";
import { HttpError, matrixCell, matrixRows, savedList } from "./read.ts";

// ---- time in Prague (the household's clock), like the Python code running locally
const prague = () => new Date().toLocaleString("sv-SE", { timeZone: "Europe/Prague" });  // "2026-09-26 10:15:30"
export const today = () => prague().slice(0, 10);
const nowMinutes = () => prague().replace(" ", "T").slice(0, 16);

// JS \b is ASCII-only ("rolí", "plenek" would never match): use a Unicode-aware end of word.
const END = "(?![\\p{L}\\p{N}_])";
const rx = (s: string) => new RegExp(s, "u");

/** Price-per-obvious-unit (port of pricetrack.infer_unit): kg for weights, l for volumes,
 *  roll/egg/piece for counts; ["pack", 1] if unsure. */
export function inferUnit(name: string): [string, number] {
  const n = name.toLowerCase().replace(/,/g, ".").replace(/×/g, "x");
  let m = n.match(rx(`(\\d+)\\s*(?:ks|pcs|kusů|plenek|rolí|role)${END}`));  // a count beats a size (bags: 60L 15ks)
  if (m) {
    const q = Number(m[1]);
    if (rx(`\\btp\\b|toalet|utěrk|rolí`).test(n)) return ["roll", q];
    if (n.includes("vejce")) return ["egg", q];
    return ["piece", q];
  }
  m = n.match(rx(`(\\d+)\\s*x\\s*(\\d+(?:\\.\\d+)?)\\s*(kg|g|ml|l)${END}`));  // multipack: 6 x 1,5 l
  if (m) {
    const k = Number(m[1]) * Number(m[2]);
    return ({ kg: ["kg", k], g: ["kg", k / 1000], ml: ["l", k / 1000], l: ["l", k] } as Record<string, [string, number]>)[m[3]];
  }
  if ((m = n.match(rx(`(\\d+(?:\\.\\d+)?)\\s*kg${END}`)))) return ["kg", Number(m[1])];
  if ((m = n.match(rx(`(\\d+(?:\\.\\d+)?)\\s*g${END}`)))) return ["kg", Number(m[1]) / 1000];
  if ((m = n.match(rx(`(\\d+(?:\\.\\d+)?)\\s*ml${END}`)))) return ["l", Number(m[1]) / 1000];
  if ((m = n.match(rx(`(\\d+(?:\\.\\d+)?)\\s*l${END}`)))) return ["l", Number(m[1])];
  return ["pack", 1];
}

const unique = (e: unknown, msg: string) => {
  if (/constraint/i.test(String((e as Error)?.message ?? e))) throw new HttpError(400, msg);
  throw e;
};
const stmt = (sql: string, ...args: unknown[]) => db().prepare(sql).bind(...(args as never[]));

// ---------------------------------------------------------------- shopping list
const CART_COLS = "row_key,row_name,unit,store_id,store_name,kind,item_name,amount,price_incl,unit_price,promo_until,club,note,url,qty,checked,added";

/** Tap a matrix cell: add that item at that store; tap again to remove. A row is on the list
 *  at one store at a time, so tapping another store's cell moves it there. */
export async function cartToggle(rowKey: string, storeId: string, kind = "store"): Promise<boolean> {
  const { stores, rows } = await matrixRows();
  const { row, snap } = matrixCell(rows, stores, rowKey, storeId, kind);
  if (!row || !snap) throw new HttpError(400, "that cell has no current price");
  const cur = await first("SELECT store_id, kind FROM cart_item WHERE row_key=?", rowKey);
  if (cur && cur.store_id === storeId && cur.kind === kind) {
    await run("DELETE FROM cart_item WHERE row_key=?", rowKey);
    return false;
  }
  await run(`INSERT INTO cart_item(${CART_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?)
             ON CONFLICT(row_key) DO UPDATE SET store_id=excluded.store_id, store_name=excluded.store_name,
               kind=excluded.kind, item_name=excluded.item_name, amount=excluded.amount,
               price_incl=excluded.price_incl, unit_price=excluded.unit_price, promo_until=excluded.promo_until,
               club=excluded.club, note=excluded.note, url=excluded.url, checked=0`,
    rowKey, row.name, row.unit, storeId, snap.store_name, kind, snap.item_name, snap.amount,
    snap.price_incl, snap.unit_price, snap.promo_until, snap.club, snap.note, snap.url, today());
  return true;
}

/** Move list lines to other stores (the list page's "apply" on a tip or scenario), keeping
 *  quantities. Every target must have a current price; all lines move in one transaction. */
export async function cartMove(moves: { row_key: string; store_id: string; kind?: string }[]): Promise<number> {
  if (!Array.isArray(moves) || !moves.length) return 0;
  if (moves.length > 300) throw new HttpError(400, "too many moves");
  const { stores, rows } = await matrixRows();
  const updates = moves.map((m) => {
    const kind = m.kind === "flyer" ? "flyer" : "store";
    const { snap } = matrixCell(rows, stores, m.row_key, m.store_id, kind);
    if (!snap) throw new HttpError(409, "prices changed since this suggestion; reload the list");
    return stmt(`UPDATE cart_item SET store_id=?, store_name=?, kind=?, item_name=?, amount=?, price_incl=?, unit_price=?,
                   promo_until=?, club=?, note=?, url=?, checked=0 WHERE row_key=?`,
      m.store_id, snap.store_name, kind, snap.item_name, snap.amount, snap.price_incl, snap.unit_price,
      snap.promo_until, snap.club, snap.note, snap.url, m.row_key);
  });
  await db().batch(updates, { mode: "immediate" });
  return updates.length;
}

export async function cartUpdate(id: number, qty?: number, checked?: boolean) {
  if (qty !== undefined && qty !== null) await run("UPDATE cart_item SET qty=? WHERE id=?", Math.max(1, Math.trunc(Number(qty))), id);
  if (checked !== undefined && checked !== null) await run("UPDATE cart_item SET checked=? WHERE id=?", checked ? 1 : 0, id);
}

export async function cartRemove(id?: number, checkedOnly = false, everything = false) {
  if (id !== undefined && id !== null) await run("DELETE FROM cart_item WHERE id=?", id);
  else if (checkedOnly) await run("DELETE FROM cart_item WHERE checked=1");
  else if (everything) await run("DELETE FROM cart_item");
}

/** Save the current list (prices included) and start a fresh one — one transaction. */
export async function cartSave(name?: string): Promise<number> {
  const items = await all("SELECT * FROM cart_item ORDER BY store_name, row_name");
  if (!items.length) throw new HttpError(400, "the list is empty");
  const total = Math.round(items.reduce((s, i) => s + (i.price_incl ?? 0) * i.qty, 0) * 100) / 100;
  const [y, m, d] = today().split("-").map(Number);
  const label = (name ?? "").trim() || `Nákup ${d}. ${m}. ${y}`;
  const [ins] = await db().batch([
    stmt("INSERT INTO saved_list(name,created,total,items) VALUES (?,?,?,?)", label, nowMinutes(), total, JSON.stringify(items)),
    stmt("DELETE FROM cart_item"),
  ], { mode: "immediate" });
  return Number(ins.lastInsertRowid);
}

/** Put a saved list back on the list: same items at the same stores, today's prices where the
 *  store still has one (else the saved snapshot). Lines already on the list win. */
export async function savedListReuse(id: number): Promise<number> {
  const [{ stores, rows }, lst, have] = await Promise.all([matrixRows(), savedList(id), all("SELECT row_key FROM cart_item")]);
  const onList = new Set(have.map((r) => r.row_key));
  const inserts = [];
  for (const saved of lst.items as Row[]) {
    if (onList.has(saved.row_key)) continue;
    const { snap } = matrixCell(rows, stores, saved.row_key, saved.store_id, saved.kind);
    const it = snap ? { ...saved, ...snap } : saved;
    inserts.push(stmt(`INSERT INTO cart_item(${CART_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)`,
      it.row_key, it.row_name, it.unit, it.store_id, it.store_name, it.kind, it.item_name, it.amount ?? null,
      it.price_incl, it.unit_price, it.promo_until ?? null, it.club ?? null, it.note ?? null, it.url ?? null, it.qty, today()));
  }
  if (inserts.length) await db().batch(inserts, { mode: "immediate" });
  return inserts.length;
}

export const savedListDelete = (id: number) => run("DELETE FROM saved_list WHERE id=?", id);

// ---------------------------------------------------------------- items
const PRODUCT_FIELDS = ["ean", "name", "category", "vat_pct", "unit", "unit_qty", "pack_note", "search_query"];
const blank = (v: unknown) => v === undefined || v === null || String(v).trim() === "";

/** New item. Unit + units-per-pack are inferred from the name (or the search text, which
 *  usually carries the pack size) unless given. */
export async function productAdd(a: Row): Promise<number> {
  const name = String(a.name ?? "").trim();
  if (!name) throw new HttpError(400, "name is required");
  const search = blank(a.search_query) ? null : String(a.search_query).trim();
  let [u, q] = inferUnit(name);
  if (u === "pack" && search) [u, q] = inferUnit(search);
  try {
    const r = await run(`INSERT INTO product(ean,name,category,vat_pct,unit,unit_qty,pack_note,search_query) VALUES (?,?,?,?,?,?,?,?)`,
      blank(a.ean) ? null : String(a.ean).trim(), name, blank(a.category) ? null : a.category,
      blank(a.vat_pct) ? null : Math.trunc(Number(a.vat_pct)), blank(a.unit) ? u : a.unit,
      blank(a.unit_qty) ? q : Number(a.unit_qty), a.pack_note ?? null, search);
    return Number(r.lastInsertRowid);
  } catch (e) { return unique(e, `an item with EAN ${a.ean} already exists`); }
}

/** Edit an item. Empty clears ean / search_query / category / pack_note; empty name, unit,
 *  unit_qty or VAT are ignored. Renaming an item without search text keeps the old name as
 *  its search text (English display name, Czech search). */
export async function productUpdate(id: number, a: Row) {
  const sets: Row = {};
  for (const k of PRODUCT_FIELDS) if (k in a) sets[k] = a[k];
  for (const k of ["name", "unit", "unit_qty", "vat_pct"]) if (k in sets && blank(sets[k])) delete sets[k];
  for (const k of ["ean", "search_query", "category", "pack_note"]) if (k in sets) sets[k] = blank(sets[k]) ? null : String(sets[k]).trim();
  if ("unit_qty" in sets) sets.unit_qty = Number(sets.unit_qty);
  if ("vat_pct" in sets) sets.vat_pct = Math.trunc(Number(sets.vat_pct));
  if (!Object.keys(sets).length) return;
  const old = await first("SELECT name, search_query FROM product WHERE id=?", id);
  if (old && "name" in sets && sets.name !== old.name && !sets.search_query && !old.search_query) sets.search_query = old.name;
  const cols = Object.keys(sets);
  const batch = [stmt(`UPDATE product SET ${cols.map((k) => k + "=?").join(", ")} WHERE id=?`, ...cols.map((k) => sets[k]), id)];
  if (sets.ean) batch.push(stmt("DELETE FROM ean_candidate WHERE product_id=?", id));
  try { await db().batch(batch, { mode: "immediate" }); }
  catch (e) { unique(e, `an item with EAN ${sets.ean} already exists`); }
}

/** Remove an item with everything matched to it (offerings + their prices, watches). */
export async function productDelete(id: number) {
  await db().batch([
    stmt("DELETE FROM offering WHERE product_id=?", id),
    stmt("DELETE FROM watch WHERE product_id=?", id),
    stmt("DELETE FROM product WHERE id=?", id),
  ], { mode: "immediate" });
}

export async function eanDismiss(productId: number) {
  await db().batch([
    stmt("DELETE FROM ean_candidate WHERE product_id=?", productId),
    stmt("UPDATE ean_lookup SET dismissed=1 WHERE product_id=?", productId),
  ], { mode: "immediate" });
}

/** Undo a match: drop the watch and this ref's offerings (with prices); a store left without a
 *  live price gets its hand-entered price back. */
export async function unlink(source: string, ref: string, productId: number): Promise<number> {
  const refOf = (sr: string) => source === "kupi" ? sr.split(":")[1] : sr;
  const rows = (await all("SELECT id, store_id, store_ref FROM offering WHERE source=? AND product_id=?", source, productId))
    .filter((r) => refOf(r.store_ref) === ref);
  const batch = [stmt("DELETE FROM watch WHERE source=? AND ref=? AND product_id=?", source, ref, productId),
    ...rows.map((r) => stmt("DELETE FROM offering WHERE id=?", r.id))];
  for (const storeId of new Set(rows.map((r) => r.store_id))) {
    batch.push(stmt(`UPDATE offering SET active=1 WHERE source='manual' AND store_id=? AND product_id=?
                     AND NOT EXISTS (SELECT 1 FROM offering o2 WHERE o2.product_id=? AND o2.store_id=?
                                     AND o2.source!='manual' AND o2.active=1)`, storeId, productId, productId, storeId));
  }
  await db().batch(batch, { mode: "immediate" });
  return rows.length;
}
