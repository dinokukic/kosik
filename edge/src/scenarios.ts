// "What would this list cost?" — cheapest / efficient run (1-3 stores) / average / most expensive /
// your picks, for the current list or a saved one. Items are costed on a common reference pack
// (median pack across stores x price per unit x quantity) so pack sizes don't skew stores.
import { all, Row } from "./db.ts";
import { MatrixRow, matrixRows, savedList } from "./read.ts";

type Line = { key: string; name: string; qty: number; pick?: { store_id: string; kind: string } };
type Option = { store: string; cost: number; flyer: boolean };
/** Switch one list line to another store (kind "flyer" = that chain's Kupi deal). */
export type Move = { row_key: string; name: string; from: string; to: string; kind: string; delta: number };

const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length ? (s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2) : NaN; };
const round = (x: number) => Math.round(x * 100) / 100;

/** Per item: what it costs at each store (reference pack x qty); flyer deals optionally count at their chain. */
function options(row: MatrixRow, qty: number, flyers: boolean) {
  const qtys = Object.values(row.cells).map((c) => Number(c.unit_qty)).filter((q) => q > 0);
  const deal = row.deal;
  const dealQty = deal && deal.unit_price ? deal.price_incl / deal.unit_price : NaN;
  const ref = qtys.length ? median(qtys) : (dealQty > 0 ? dealQty : 1);
  const opts = new Map<string, Option>(), choices: Option[] = [];  // opts: best per store; choices: every pickable cell
  for (const [store, c] of Object.entries(row.cells)) {
    const o = { store, cost: c.unit_price * ref * qty, flyer: false };
    opts.set(store, o); choices.push(o);
  }
  if (flyers && deal) {
    const o = { store: deal.store_id, cost: deal.unit_price * ref * qty, flyer: true }, cur = opts.get(deal.store_id);
    choices.push(o);
    if (!cur || o.cost < cur.cost) opts.set(deal.store_id, o);
  }
  return { ref, opts, choices };
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
    const { ref, opts: o, choices } = row ? options(row, l.qty, flyers) : { ref: 0, opts: new Map<string, Option>(), choices: [] as Option[] };
    if (row?.deal) names[row.deal.store_id] ??= row.deal.store_name ?? row.deal.store_id;
    // what the line costs where it's picked now (null: not priced there any more)
    const now = l.pick ? choices.find((c) => c.store === l.pick!.store_id && c.flyer === (l.pick!.kind === "flyer")) ?? null : null;
    return { ...l, unit: row?.unit, ref, opts: o, choices, now };
  });
  const priced = items.filter((i) => i.opts.size);
  const costs = (i: typeof priced[0]) => [...i.opts.values()].map((o) => o.cost);
  const sum = (f: (i: typeof priced[0]) => number) => round(priced.reduce((s, i) => s + f(i), 0));

  const cheapest = sum((i) => Math.min(...costs(i)));
  const live = !opts.saved;  // moves and tips only make sense for the list you can still change
  const movesWithin = (allowed?: Set<string>) => live
    ? priced.map((i) => { const c = bestChoice(i, allowed); return c ? moveTo(i, c) : null; }).filter((m): m is Move => !!m) : [];
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
      moves: best.covered === priced.length ? movesWithin(new Set(best.stores)) : [],
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
    names, tips: live ? tips(items, names) : [],
    basket: opts.saved ? `saved list #${opts.saved}` : "current list", flyers,
    items: items.length, priced: priced.length,
    only_one_store: priced.filter((i) => i.opts.size === 1).length,
    unpriced: items.filter((i) => !i.opts.size).map((i) => i.name),
    scenarios: {
      cheapest: { total: cheapest, stores: cheapestStores.size, list: [...cheapestStores].map((s) => names[s] ?? s), moves: movesWithin() },
      efficient,
      average: { total: sum((i) => costs(i).reduce((a, b) => a + b, 0) / costs(i).length) },
      most_expensive: { total: sum((i) => Math.max(...costs(i))) },
      your_picks: { total: yoursMissing ? null : round(yours ?? 0), missing: yoursMissing },
    },
    lines: priced.map((i) => ({ name: i.name, qty: i.qty, unit: i.unit, ref_pack: round(i.ref),
      options: Object.fromEntries([...i.opts.values()].sort((a, b) => a.cost - b.cost).map((o) => [names[o.store] ?? o.store, round(o.cost) + (o.flyer ? " (flyer)" : "")])) })),
  };
}

type Item = { key: string; name: string; pick?: Line["pick"]; choices: Option[]; now: Option | null };

const bestChoice = (i: Item, allowed?: Set<string>) =>
  i.choices.filter((c) => !allowed || allowed.has(c.store)).reduce<Option | null>((a, b) => (!a || b.cost < a.cost ? b : a), null);
/** The move that puts line i at choice c (null if it's already there). */
const moveTo = (i: Item, c: Option): Move | null =>
  i.pick && i.pick.store_id === c.store && (i.pick.kind === "flyer") === c.flyer ? null
    : { row_key: i.key, name: i.name, from: i.pick?.store_id ?? "", to: c.store, kind: c.flyer ? "flyer" : "store",
        delta: round(c.cost - (i.now?.cost ?? c.cost)) };
export type Tip = { kind: "cheaper" | "drop"; store?: string; items: string[]; to: string[]; delta: number; moves: Move[] };

/** Ways to improve the current picks without adding a store:
 *  - cheaper: lines that cost less at a store you're visiting anyway;
 *  - drop: a store whose few lines can all be bought at your other stores (one stop fewer, +/- X Kč). */
function tips(items: Item[], names: Record<string, string>): Tip[] {
  const visiting = new Set(items.map((i) => i.pick?.store_id).filter((s): s is string => !!s));
  const out: Tip[] = [];
  const cheaper = items.flatMap((i) => {
    if (!i.now) return [];
    const c = bestChoice(i, visiting);
    const m = c && c.cost < i.now.cost - 0.5 ? moveTo(i, c) : null;
    return m ? [m] : [];
  });
  if (cheaper.length) out.push({ kind: "cheaper", items: cheaper.map((m) => m.name), to: [...new Set(cheaper.map((m) => names[m.to] ?? m.to))],
    delta: round(cheaper.reduce((s, m) => s + m.delta, 0)), moves: cheaper });
  if (visiting.size > 1) {
    for (const store of visiting) {
      const lines = items.filter((i) => i.pick?.store_id === store);
      const others = new Set([...visiting].filter((s) => s !== store));
      const moves: Move[] = [];
      for (const i of lines) {
        const c = i.now ? bestChoice(i, others) : null;
        const m = c && moveTo(i, c);
        if (!m) { moves.length = 0; break; }  // something here can't be bought elsewhere: the stop stays
        moves.push(m);
      }
      if (moves.length === lines.length && moves.length) out.push({ kind: "drop", store: names[store] ?? store, items: moves.map((m) => m.name),
        to: [...new Set(moves.map((m) => names[m.to] ?? m.to))], delta: round(moves.reduce((s, m) => s + m.delta, 0)), moves });
    }
  }
  // cheapest first; drops of small stops before big ones at the same cost
  return out.sort((a, b) => a.delta - b.delta || a.items.length - b.items.length).slice(0, 4);
}

async function basketLines(saved?: number): Promise<Line[]> {
  const src: Row[] = saved ? (await savedList(saved)).items : await all("SELECT * FROM cart_item ORDER BY row_name");
  return src.map((c) => ({ key: c.row_key, name: c.row_name, qty: Number(c.qty) || 1, pick: { store_id: c.store_id, kind: c.kind } }));
}
