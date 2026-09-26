// "What would this list cost?" — cheapest / efficient run (1-3 stores) / average / most expensive /
// your picks, for the current list or a saved one. Items are costed on a common reference pack
// (median pack across stores x price per unit x quantity) so pack sizes don't skew stores.
import { all, Row } from "./db.ts";
import { MatrixRow, matrixRows, savedList } from "./read.ts";

type Line = { key: string; name: string; qty: number; pick?: { store_id: string; kind: string } };
type Option = { store: string; cost: number; flyer: boolean };

const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length ? (s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2) : NaN; };
const round = (x: number) => Math.round(x * 100) / 100;

/** Per item: what it costs at each store (reference pack x qty); flyer deals optionally count at their chain. */
function options(row: MatrixRow, qty: number, flyers: boolean) {
  const qtys = Object.values(row.cells).map((c) => Number(c.unit_qty)).filter((q) => q > 0);
  const deal = row.deal;
  const dealQty = deal && deal.unit_price ? deal.price_incl / deal.unit_price : NaN;
  const ref = qtys.length ? median(qtys) : (dealQty > 0 ? dealQty : 1);
  const opts = new Map<string, Option>();
  for (const [store, c] of Object.entries(row.cells)) opts.set(store, { store, cost: c.unit_price * ref * qty, flyer: false });
  if (flyers && deal) {
    const cost = deal.unit_price * ref * qty, cur = opts.get(deal.store_id);
    if (!cur || cost < cur.cost) opts.set(deal.store_id, { store: deal.store_id, cost, flyer: true });
  }
  return { ref, opts };
}

function* combos<T>(xs: T[], k: number, start = 0, acc: T[] = []): Generator<T[]> {
  if (acc.length === k) { yield acc; return; }
  for (let i = start; i < xs.length; i++) yield* combos(xs, k, i + 1, [...acc, xs[i]]);
}

export async function scenarios(opts: { saved?: number; flyers?: boolean }) {
  const flyers = opts.flyers ?? true;
  const [{ stores, rows }, lines] = await Promise.all([matrixRows(), basketLines(opts.saved)]);
  const names: Record<string, string> = Object.fromEntries(stores.map((s) => [s.id, s.name]));
  const byKey = new Map(rows.map((r) => [r.key, r]));
  const items = lines.map((l) => {
    const row = byKey.get(l.key);
    const { ref, opts: o } = row ? options(row, l.qty, flyers) : { ref: 0, opts: new Map<string, Option>() };
    if (row?.deal) names[row.deal.store_id] ??= row.deal.store_name ?? row.deal.store_id;
    return { ...l, unit: row?.unit, ref, opts: o };
  });
  const priced = items.filter((i) => i.opts.size);
  const costs = (i: typeof priced[0]) => [...i.opts.values()].map((o) => o.cost);
  const sum = (f: (i: typeof priced[0]) => number) => round(priced.reduce((s, i) => s + f(i), 0));

  const cheapest = sum((i) => Math.min(...costs(i)));
  const cheapestStores = new Set(priced.map((i) => [...i.opts.values()].reduce((a, b) => (b.cost < a.cost ? b : a)).store));

  // efficient run: best k-store combination, by coverage first, then total
  const storeIds = [...new Set(priced.flatMap((i) => [...i.opts.keys()]))];
  const efficient = [1, 2, 3].map((k) => {
    let best: { stores: string[]; covered: number; total: number } | null = null;
    for (const combo of combos(storeIds, k)) {
      let covered = 0, total = 0;
      for (const i of priced) {
        const c = combo.map((s) => i.opts.get(s)?.cost).filter((x): x is number => x !== undefined);
        if (c.length) { covered++; total += Math.min(...c); }
      }
      if (!best || covered > best.covered || (covered === best.covered && total < best.total)) best = { stores: combo, covered, total };
    }
    return best && { k, stores: best.stores.map((s) => names[s] ?? s), covered: best.covered, of: priced.length, total: round(best.total),
      // compare like with like: the cheapest possible price of the same covered items
      vs_cheapest: round(best.total - priced.filter((i) => best!.stores.some((s) => i.opts.has(s))).reduce((s, i) => s + Math.min(...costs(i)), 0)) };
  }).filter(Boolean);

  // your picks: each line at the store you chose on the list (null if it's not priced there now)
  let yours: number | null = 0, yoursMissing = 0;
  for (const i of priced) {
    const o = i.pick ? i.opts.get(i.pick.store_id) : undefined;
    if (o) yours += o.cost; else yoursMissing++;
  }

  return {
    basket: opts.saved ? `saved list #${opts.saved}` : "current list", flyers,
    items: items.length, priced: priced.length,
    only_one_store: priced.filter((i) => i.opts.size === 1).length,
    unpriced: items.filter((i) => !i.opts.size).map((i) => i.name),
    scenarios: {
      cheapest: { total: cheapest, stores: cheapestStores.size, list: [...cheapestStores].map((s) => names[s] ?? s) },
      efficient,
      average: { total: sum((i) => costs(i).reduce((a, b) => a + b, 0) / costs(i).length) },
      most_expensive: { total: sum((i) => Math.max(...costs(i))) },
      your_picks: { total: yoursMissing ? null : round(yours ?? 0), missing: yoursMissing },
    },
    lines: priced.map((i) => ({ name: i.name, qty: i.qty, unit: i.unit, ref_pack: round(i.ref),
      options: Object.fromEntries([...i.opts.values()].sort((a, b) => a.cost - b.cost).map((o) => [names[o.store] ?? o.store, round(o.cost) + (o.flyer ? " (flyer)" : "")])) })),
  };
}

async function basketLines(saved?: number): Promise<Line[]> {
  const src: Row[] = saved ? (await savedList(saved)).items : await all("SELECT * FROM cart_item ORDER BY row_name");
  return src.map((c) => ({ key: c.row_key, name: c.row_name, qty: Number(c.qty) || 1, pick: { store_id: c.store_id, kind: c.kind } }));
}
