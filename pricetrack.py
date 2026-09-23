#!/usr/bin/env python3
"""
pricetrack - a personal grocery price tracker.

Three layers (see CLAUDE.md and bunny/schema.sql):
  need      your abstraction ("4-ply toilet paper"); the basket is a list of needs
  product   same physical item across stores, joined by EAN
  offering  one listing at one store (store's own id, matched ONCE) + price history

Store ids are resolved once with `match` (search the store, you pick the right pack),
cached in `offering`, and fetched by id forever after.

Usage:
    python pricetrack.py init                          schema + stores + watchlist products
    python pricetrack.py seed                          offline hand-verified prices (receipt etc.)
    python pricetrack.py search rohlik "pampers 1"     candidates from a source (rohlik | kupi)
    python pricetrack.py match rohlik 8006540858073    search by product name, pick, cache
    python pricetrack.py match rohlik --need "4-ply TP" --query "toaletní papír 4vrstvý"
    python pricetrack.py fetch [--source rohlik]       refresh prices of matched offerings
    python pricetrack.py need add "4-ply TP" --unit roll
    python pricetrack.py match kupi 8593803421216      flyer offers at every chain (aggregator)
    python pricetrack.py need link "4-ply TP" 8584014858502 [--offering 12]
    python pricetrack.py store member kaufland off     ignore Kaufland Card prices
    python pricetrack.py matrix                        rows x stores, cheapest marked
    python pricetrack.py export                        writes dashboard/prices.json
"""
import argparse, csv, json, os, re, sqlite3, time, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "prices.db"
SCHEMA = ROOT / "bunny" / "schema.sql"
WATCHLIST = ROOT / "watchlist.csv"
EXPORT = ROOT / "dashboard" / "prices.json"
UA = "personal-pricetrack/1.0"

STORES = [  # id, name, url, prices_ex_vat
    ("rohlik", "Rohlík", "https://www.rohlik.cz", 0),
    ("makro", "Makro", "https://sortiment.makro.cz", 1),
    ("kaufland", "Kaufland", "https://www.kaufland.cz", 0),
    ("tesco", "Tesco", "https://www.itesco.cz", 0),
    ("albert", "Albert", "https://www.albert.cz", 0),
    ("billa", "Billa", "https://www.billa.cz", 0),
    ("lidl", "Lidl", "https://www.lidl.cz", 0),
    ("penny", "Penny", "https://www.penny.cz", 0),
    ("globus", "Globus", "https://www.globus.cz", 0),
    ("dm", "dm", "https://www.dm.cz", 0),
    ("rossmann", "Rossmann", "https://www.rossmann.cz", 0),
    ("teta", "Teta", "https://www.tetadrogerie.cz", 0),
    ("alza", "Alza", "https://www.alza.cz", 0),
    ("rossmann_pl", "Rossmann PL", "https://www.rossmann.pl", 0),
    ("lindt", "Lindt", "https://www.lindt.cz", 0),
    ("heureka", "Heureka (min)", "https://www.heureka.cz", 0),
]

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON"); return c

def today(): return dt.date.today().isoformat()

# ---------------------------------------------------------------- units
def infer_unit(name, category):
    """Price-per-obvious-unit, kept simple: kg for weights, l for volumes,
    roll/egg/piece for counts. Returns (unit, unit_qty); ('pack', 1) if unsure."""
    n = name.lower().replace(",", ".")
    if m := re.search(r"(\d+)\s*(?:ks|pcs)\b", n):  # a count beats a size (bags: 60L 15ks)
        q = int(m[1])
        if re.search(r"\btp\b|toalet|utěrk|rolí", n): return "roll", q
        if "vejce" in n: return "egg", q
        return "piece", q
    if m := re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*g\b", n):
        return "kg", int(m[1]) * float(m[2]) / 1000
    if m := re.search(r"(\d+(?:\.\d+)?)\s*kg\b", n): return "kg", float(m[1])
    if m := re.search(r"(\d+(?:\.\d+)?)\s*g\b", n): return "kg", float(m[1]) / 1000
    if m := re.search(r"(\d+(?:\.\d+)?)\s*ml\b", n): return "l", float(m[1]) / 1000
    if m := re.search(r"(\d+(?:\.\d+)?)\s*l\b", n): return "l", float(m[1])
    return "pack", 1

# ---------------------------------------------------------------- writes
def upsert_offering(c, store_id, store_ref, name, product_id=None, url="", unit_qty=None,
                    source="manual", need_id=None, store_name=None):
    c.execute("INSERT OR IGNORE INTO store(id,name) VALUES (?,?)",
              (store_id, store_name or store_id.title()))
    c.execute("""INSERT INTO offering(store_id,store_ref,source,name,product_id,need_id,url,unit_qty)
                 VALUES (?,?,?,?,?,?,?,?)
                 ON CONFLICT(store_id,store_ref) DO UPDATE SET name=excluded.name,
                   url=excluded.url, source=excluded.source,
                   product_id=COALESCE(excluded.product_id, offering.product_id),
                   need_id=COALESCE(excluded.need_id, offering.need_id),
                   unit_qty=COALESCE(excluded.unit_qty, offering.unit_qty)""",
              (store_id, str(store_ref), source, name, product_id, need_id, url, unit_qty))
    return c.execute("SELECT id FROM offering WHERE store_id=? AND store_ref=?",
                     (store_id, str(store_ref))).fetchone()[0]

def record(c, offering_id, price_incl, is_promo=False, promo_until=None, ts=None,
           valid_from=None, club=None, note=None, ref=""):
    ts = ts or today()
    c.execute("""INSERT OR REPLACE INTO price(offering_id,ts,ref,price_incl,is_promo,valid_from,
                                              promo_until,club,note)
                 VALUES (?,?,?,?,?,?,?,?,?)""",
              (offering_id, ts, str(ref), round(price_incl, 2), int(bool(is_promo)),
               valid_from or ts, promo_until, club, note))

# ---------------------------------------------------------------- init / seed
def init():
    c = conn(); c.executescript(SCHEMA.read_text(encoding="utf-8"))
    c.executemany("""INSERT INTO store(id,name,url,prices_ex_vat) VALUES (?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url,
                       prices_ex_vat=excluded.prices_ex_vat""", STORES)
    with open(WATCHLIST, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            unit, qty = infer_unit(r["name"], r["category"])
            if r.get("unit"): unit = r["unit"]
            if r.get("unit_qty"): qty = float(r["unit_qty"])
            c.execute("""INSERT INTO product(ean,name,category,vat_pct,unit,unit_qty,pack_note)
                         VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(ean) DO UPDATE SET name=excluded.name,
                           category=excluded.category, vat_pct=excluded.vat_pct,
                           unit=excluded.unit, unit_qty=excluded.unit_qty,
                           pack_note=excluded.pack_note""",
                      (r["ean"], r["name"], r["category"], int(r["vat_pct"]), unit, qty,
                       r["pack_note"]))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM product").fetchone()[0]; c.close()
    print(f"init ok -> {DB} ({n} products)")

# Hand-verified prices so the matrix has data before any live fetch.
VERIFIED = {
    "8006540858073": [("dm", 299.0, False), ("rossmann_pl", 335.0, False)],
    "7610400013864": [("heureka", 129.0, False), ("lindt", 219.9, False)],
    "8593803421216": [("kaufland", 34.9, True)],
    "8594006091329": [("tesco", 34.9, True)],
    "4014400762235": [("penny", 29.9, True)],
    "8718951801400": [("dm", 159.0, False)],
    "8720181481529": [("teta", 94.9, True)],
    "8584014858502": [("alza", 179.0, False)],
    "8594001021666": [("tesco", 14.9, True)],
    "8435409374261": [("kaufland", 167.5, False)],
    "8411135006829": [("heureka", 459.0, False)],
}

def seed():
    """Makro receipt prices + a few hand-checked ones. Offerings keyed 'ean:<ean>'
    since we don't know those stores' own ids yet."""
    c = conn()
    prods = {r["ean"]: r for r in c.execute("SELECT id, ean, name FROM product")}
    with open(WATCHLIST, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["makro_price_incl"] and r["ean"] in prods:
                p = prods[r["ean"]]
                oid = upsert_offering(c, "makro", f"ean:{p['ean']}", p["name"], p["id"],
                                      "https://sortiment.makro.cz")
                record(c, oid, float(r["makro_price_incl"]))
    for ean, quotes in VERIFIED.items():
        p = prods[ean]
        for store, price, promo in quotes:
            oid = upsert_offering(c, store, f"ean:{ean}", p["name"], p["id"])
            record(c, oid, price, promo)
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM price").fetchone()[0]; c.close()
    print(f"seed ok ({n} prices)")

# ---------------------------------------------------------------- sources
# A SOURCE is where prices come from; a STORE is who sells. Rohlík is both. Kupi is an
# aggregator of chain flyers: its offers are recorded under the chain (kaufland, albert...),
# never under a 'kupi' store.
#
# source.search(query) -> [candidate]      candidate = dict(ref, name, url, offers=[offer])
# source.fetch([ref])  -> {ref: [offer]}   ref = what a match caches (rohlik id, kupi slug)
# source.ref_of(store_ref) -> ref
# offer = dict(store_id, store_name, store_ref, name, amount, url, price_incl, is_promo,
#              valid_from, promo_until, club, note, ref)

def http_get(url, params=None, headers=None, json=True):
    import requests
    h = {"User-Agent": UA, **(headers or {})}
    r = requests.get(url, params=params, headers=h, timeout=20)
    r.raise_for_status()
    return r.json() if json else r.text

class Rohlik:
    """VERIFIED live 2026-09-23. No EAN search: name -> autocomplete ids -> product cards.
    Prices include VAT. ROHLIK_COOKIE pins prices to your delivery region (Prague 1)."""
    BASE = "https://www.rohlik.cz"

    def _get(self, path, params):
        h = {"Accept": "application/json"}
        if os.environ.get("ROHLIK_COOKIE"): h["Cookie"] = os.environ["ROHLIK_COOKIE"]
        return http_get(self.BASE + path, params, h)

    def _cards(self, ids):
        out = []
        for i in range(0, len(ids), 20):
            params = [("products", x) for x in ids[i:i + 20]] + [("categoryType", "normal")]
            out += self._get("/api/v1/products/card", params)
            if i + 20 < len(ids): time.sleep(1.0)
        return out

    def _offer(self, card):
        p = card.get("prices") or {}
        sale, orig = p.get("salePrice"), p.get("originalPrice")
        cur = sale if sale is not None else orig
        if cur is None: return None
        promo = sale is not None and orig is not None and sale < orig
        till = p.get("saleValidTill")
        ref = str(card["productId"])
        return dict(store_id="rohlik", store_name="Rohlík", store_ref=ref,
                    name=card.get("name", ""), amount=card.get("textualAmount", ""),
                    url=f"{self.BASE}/{ref}", price_incl=float(cur), is_promo=promo,
                    valid_from=None, promo_until=str(till)[:10] if promo and till else None,
                    club=None, note=None, ref="")

    def search(self, query, n=12):
        ids = self._get("/services/frontend-service/autocomplete",
                        {"search": query, "referer": "whisperer", "companyId": 1}
                        ).get("productIds", [])[:n]
        return [dict(ref=o["store_ref"], name=o["name"], url=o["url"], offers=[o])
                for o in map(self._offer, self._cards(ids)) if o]

    def fetch(self, refs):
        return {o["store_ref"]: [o] for o in map(self._offer, self._cards(list(refs))) if o}

    def ref_of(self, store_ref): return store_ref

class Kupi:
    """VERIFIED live 2026-09-23. AGGREGATOR of chain flyers (Tesco, Albert, Kaufland, Billa,
    Penny, Lidl, Globus...). Server-rendered HTML, no JSON API, no login; location = Praha.
      search:  GET /hledej?f=<q>        -> products (<a data-product-id data-product=slug>)
      product: GET /sleva/<slug>         -> every current offer as <div class="discount_row">
    Each row: data-discount (offer id), data-key (pack, '250-g'), chain via /letaky/<chain>,
    price, pack amount, validity text, optional club condition + note. Rows repeat across
    page sections -> dedupe by offer id. One chain can have several offers for one product
    (card price vs regular, different flyers); all are stored, the view picks per today."""
    BASE = "https://www.kupi.cz"
    CHAINS = {"penny-market": "penny", "teta-drogerie": "teta"}  # kupi slug -> store id

    @staticmethod
    def _text(fragment):
        import html
        s = re.sub(r"<svg.*?</svg>|<[^>]+>", " ", fragment, flags=re.S)
        return re.sub(r"\s+", " ", html.unescape(s)).strip()

    @staticmethod
    def _dates(text):
        """'platí do úterý 29. 9.' | 'čt 24. 9. – ne 27. 9.' | 'dnes končí' | 'zítra končí'
        -> (valid_from, valid_to) as ISO dates (None = unknown/now)."""
        t0 = dt.date.today()
        def mk(d, m, y):
            day = dt.date(int(y) if y else t0.year, int(m), int(d))
            # no year on the flyer and half a year in the past -> it's next year's date
            return day.replace(year=day.year + 1) if not y and (t0 - day).days > 180 else day
        if "dnes končí" in text: return None, t0.isoformat()
        if "zítra končí" in text: return None, (t0 + dt.timedelta(days=1)).isoformat()
        ds = [mk(d, m, y) for d, m, y in re.findall(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?", text)]
        if len(ds) >= 2: return ds[0].isoformat(), ds[-1].isoformat()
        if len(ds) == 1:
            return (ds[0].isoformat(), None) if re.search(r"\bod\b", text) else (None, ds[0].isoformat())
        return None, None

    def _rows(self, page):
        """All offers on a page, deduped: {product_num_id: [offer]} (store_ref filled later)."""
        out, seen = {}, set()
        for chunk in page.split('<div class="discount_row')[1:]:
            did = re.search(r'data-discount="(\d+)"', chunk)
            if not did or did[1] in seen: continue
            seen.add(did[1])
            pid = re.search(r'data-product="(\d+)"', chunk)
            key = re.search(r'data-key="([^"]*)"', chunk)
            shop = re.search(r'discounts_shop_name">\s*<a href="/letaky/([^"/?]+)"[^>]*title="([^"]*)"', chunk)
            price = re.search(r'discount_price_value">([^<]+)<', chunk)
            if not (pid and shop and price): continue
            amount = re.search(r'discount_amount">(.*?)</div>', chunk, re.S)
            valid = re.search(r'discounts_validity[^"]*">(.*?)</div>\s*</div>', chunk, re.S)
            club = re.search(r'discounts_club">(.*?)</div>', chunk, re.S)
            note = re.search(r'discount_note">(.*?)</div>', chunk, re.S)
            vf, vt = self._dates(self._text(valid[1]) if valid else "")
            chain = self.CHAINS.get(shop[1], shop[1])
            out.setdefault(pid[1], []).append(dict(
                store_id=chain, store_name=self._text(shop[2]), store_ref=None,
                key=key[1] if key else "vse",
                name="", amount=self._text(amount[1]).lstrip("/ ").strip() if amount else "",
                url="", price_incl=float(self._text(price[1]).replace("Kč", "")
                                         .replace(" ", "").replace(",", ".")),
                is_promo=True, valid_from=vf, promo_until=vt,
                club=self._text(club[1]) or None if club else None,
                note=self._text(note[1]) or None if note else None, ref=did[1]))
        return out

    def _products(self, page):
        """{num_id: (slug, name)} from product links on a page."""
        found = {}
        for m in re.finditer(r'<a href="/sleva/([^"]+)" title="([^"]*)"[^>]*data-product-id="(\d+)"', page):
            found.setdefault(m[3], (m[1], self._text(m[2])))
        return found

    def _finish(self, slug, name, offers):
        for o in offers:
            o.update(store_ref=f"kupi:{slug}:{o['key']}", name=f"{name} {o['amount']}".strip(),
                     url=f"{self.BASE}/sleva/{slug}")
        return offers

    def search(self, query):
        page = http_get(f"{self.BASE}/hledej", {"f": query}, json=False)
        rows, prods = self._rows(page), self._products(page)
        return [dict(ref=slug, name=name, url=f"{self.BASE}/sleva/{slug}",
                     offers=self._finish(slug, name, rows.get(pid, [])))
                for pid, (slug, name) in prods.items()]

    def fetch(self, refs):
        out = {}
        for i, slug in enumerate(refs):
            if i: time.sleep(1.5)  # be polite: one product page at a time
            page = http_get(f"{self.BASE}/sleva/{slug}", json=False)
            name = re.search(r'"@type":"Product","name":"([^"]+)"', page)
            name = name[1] if name else slug
            out[slug] = self._finish(slug, name, [o for offers in self._rows(page).values()
                                                  for o in offers])
        return out

    def ref_of(self, store_ref): return store_ref.split(":")[1]

# Heureka / Makro: not wired yet. Each needs the live-inspect treatment Rohlík and Kupi got
# before it gets a search()/fetch() adapter. Makro: needs MAKRO_COOKIE, shows bez DPH ->
# add product.vat_pct back.

SOURCES = {"rohlik": Rohlik, "kupi": Kupi}

def source(name):
    if name not in SOURCES:
        raise SystemExit(f"no live source {name!r} yet (have: {', '.join(SOURCES)})")
    return SOURCES[name]()

# ---------------------------------------------------------------- search / match / fetch
def fmt_offer(o):
    when = f" do {o['promo_until'][8:10]}.{o['promo_until'][5:7]}." if o["promo_until"] else ""
    if o["valid_from"] and o["valid_from"] > today():
        when = f" od {o['valid_from'][8:10]}.{o['valid_from'][5:7]}." + when
    return (f"{o['store_id']} {o['price_incl']:.2f}/{o['amount'] or '?'}"
            + (" akce" if o["is_promo"] else "") + when + (" [requires card]" if o["club"] else ""))

def show_candidates(cands):
    for i, cd in enumerate(cands, 1):
        print(f"  {i:2}. [{cd['ref']}] {cd['name'][:56]}")
        for o in cd["offers"][:6]:
            print(f"        {fmt_offer(o)}")
        if len(cd["offers"]) > 6: print(f"        ... +{len(cd['offers']) - 6} more")

def search(src, query):
    cands = source(src).search(query)
    print(f"{src}: {query!r} -> {len(cands)} candidates")
    show_candidates(cands)
    return cands

def save_offers(c, src, offers, product=None, need=None, qty=None):
    """Create/refresh an offering per (store, store_ref) and record each offer as a price row.
    unit_qty comes from the pack amount when it's in the row's unit; `qty` forces it."""
    target = product or need
    for o in offers:
        unit, guess = infer_unit(f"{o['name']} {o['amount']}", target["category"] if target else None)
        q = qty if qty is not None else (guess if target and unit == target["unit"] else None)
        if product is not None and q is not None and abs(q - product["unit_qty"]) < 1e-9:
            q = None  # same pack as the product: inherit
        if need is not None and q is None:
            print(f"  warning: no per-{need['unit']} quantity in {o['name']!r}; pass --qty")
        oid = upsert_offering(c, o["store_id"], o["store_ref"], o["name"],
                              product["id"] if product else None, o["url"], q, src,
                              need["id"] if need else None, o["store_name"])
        record(c, oid, o["price_incl"], o["is_promo"], o["promo_until"],
               valid_from=o["valid_from"], club=o["club"], note=o["note"], ref=o["ref"])

def match(src, ean=None, need=None, query=None, pick=None, qty=None):
    """Add-a-product == match: search the source, you pick the right item, we cache its id.
    Link to a product (same EAN, `ean`) or, for a different brand/pack that satisfies the
    same need, only to the need (`need`). A Kupi pick brings every chain's flyer offer."""
    c = conn()
    if ean:
        product = c.execute("SELECT * FROM product WHERE ean=?", (ean,)).fetchone()
        if not product: raise SystemExit(f"no product with EAN {ean}")
        target, need_row = product, None
    else:
        need_row = c.execute("SELECT * FROM need WHERE name=?", (need,)).fetchone()
        if not need_row: raise SystemExit(f"no need named {need!r} (run: need add)")
        target, product = need_row, None
    cands = search(src, query or target["name"])
    if not cands: return
    if pick is None:
        raw = input(f"\nWhich is '{target['name']}'?  number, or blank to skip: ").strip()
        if not raw: return
        pick = int(raw)
    cd = cands[pick - 1]
    offers = source(src).fetch([cd["ref"]]).get(cd["ref"], cd["offers"])
    save_offers(c, src, offers, product, need_row, qty)
    c.commit(); c.close()
    print(f"matched {target['name']} -> {src}:{cd['ref']} ({cd['name']}), "
          f"{len(offers)} offer(s): " + "; ".join(fmt_offer(o) for o in offers[:4]))

def fetch(which):
    c = conn()
    for src in which or list(SOURCES):
        s = source(src)
        rows = c.execute("SELECT * FROM offering WHERE source=? AND active=1", (src,)).fetchall()
        if not rows:
            print(f"  {src}: nothing matched yet (run: match {src} <ean>)"); continue
        groups = {}
        for r in rows: groups.setdefault(s.ref_of(r["store_ref"]), []).append(r)
        try:
            got = s.fetch(list(groups))
        except Exception as e:  # fail soft per source
            print(f"  [{src}] {e}"); continue
        for ref, members in groups.items():
            offers = got.get(ref) or []
            if not offers:
                print(f"  {src:6} {members[0]['name'][:44]:44} (no current offers)"); continue
            m = members[0]  # new chains/packs inherit what this ref was matched to
            product = c.execute("SELECT * FROM product WHERE id=?", (m["product_id"],)).fetchone() \
                if m["product_id"] else None
            need = c.execute("SELECT * FROM need WHERE id=?", (m["need_id"],)).fetchone() \
                if m["need_id"] else None
            save_offers(c, src, offers, product, need)
            for o in offers:
                print(f"  {src:6} {o['name'][:44]:44} {fmt_offer(o)}")
        c.commit()
    c.close(); print("fetch done")

# ---------------------------------------------------------------- needs
def need_add(name, unit, category):
    c = conn()
    c.execute("INSERT INTO need(name,unit,category) VALUES (?,?,?)", (name, unit, category))
    c.commit(); c.close(); print(f"need added: {name} (per {unit})")

def need_link(name, eans, offerings):
    c = conn()
    n = c.execute("SELECT id, unit FROM need WHERE name=?", (name,)).fetchone()
    if not n: raise SystemExit(f"no need named {name!r}")
    for ean in eans:
        p = c.execute("SELECT unit FROM product WHERE ean=?", (ean,)).fetchone()
        if not p: print(f"  no product {ean}"); continue
        if p["unit"] != n["unit"]:
            print(f"  warning: {ean} is priced per {p['unit']}, need is per {n['unit']}")
        c.execute("UPDATE product SET need_id=? WHERE ean=?", (n["id"], ean))
    for oid in offerings:
        c.execute("UPDATE offering SET need_id=? WHERE id=?", (n["id"], oid))
    c.commit(); c.close(); print(f"linked {len(eans)} products, {len(offerings)} offerings -> {name}")

def store_member(store_id, on):
    c = conn()
    n = c.execute("UPDATE store SET member=? WHERE id=?", (int(on), store_id)).rowcount
    c.commit(); c.close()
    print(f"{store_id}: club/card prices {'count' if on else 'ignored'}" if n else f"no store {store_id!r}")

# ---------------------------------------------------------------- matrix
def matrix_rows():
    c = conn()
    cells = [dict(r) for r in c.execute("SELECT * FROM v_matrix ORDER BY category, row_name")]
    stores = [dict(r) for r in c.execute(
        "SELECT id, name FROM store WHERE id IN (SELECT DISTINCT store_id FROM v_matrix) "
        "ORDER BY id")]
    c.close()
    rows = {}
    for cell in cells:
        r = rows.setdefault(cell["row_key"], dict(key=cell["row_key"], name=cell["row_name"],
                            category=cell["category"], unit=cell["unit"], cells={}))
        r["cells"][cell["store_id"]] = {k: cell[k] for k in (
            "offering_id", "offering_name", "source", "price_incl", "unit_qty", "unit_price",
            "is_promo", "promo_until", "club", "note", "ts", "url")} | {"is_cheapest": bool(cell["is_cheapest"])}
    return stores, list(rows.values())

def matrix():
    stores, rows = matrix_rows()
    ids = [s["id"] for s in stores]
    print("\n%-34s %-6s" % ("ROW", "PER") + "".join(f"{s[:8]:>9}" for s in ids))
    print("-" * (41 + 9 * len(ids)))
    for r in rows:
        line = "%-34s %-6s" % (r["name"][:34], r["unit"])
        for s in ids:
            cell = r["cells"].get(s)
            if not cell: line += f"{'·':>9}"; continue
            mark = "*" if cell["is_cheapest"] else " "
            line += f"{cell['unit_price']:8.2f}{mark}"
        print(line)
    print("\n* = cheapest per unit in the row. Prices incl. VAT, latest capture per offering.\n")

def export():
    stores, rows = matrix_rows()
    EXPORT.parent.mkdir(exist_ok=True)
    EXPORT.write_text(json.dumps(dict(
        generated=dt.datetime.now().isoformat(timespec="minutes"), stores=stores, rows=rows),
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"export ok -> {EXPORT} ({len(rows)} rows x {len(stores)} stores)")

# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="Košík price tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "seed", "matrix", "export"): sub.add_parser(name)
    s = sub.add_parser("search"); s.add_argument("source", choices=list(SOURCES))
    s.add_argument("query")
    m = sub.add_parser("match"); m.add_argument("source", choices=list(SOURCES))
    m.add_argument("ean", nargs="?", help="link to this product (same item)")
    m.add_argument("--need", help="link only to this need (different brand/pack)")
    m.add_argument("--query", help="search text (default: product/need name)")
    m.add_argument("--qty", type=float, help="units per pack, if it can't be read from the name")
    m.add_argument("--pick", type=int, help="candidate number (skip the prompt)")
    f = sub.add_parser("fetch"); f.add_argument("--source", help="comma list, e.g. rohlik,kupi")
    st = sub.add_parser("store"); stsub = st.add_subparsers(dest="scmd", required=True)
    sm = stsub.add_parser("member", help="do you hold this chain's loyalty card?")
    sm.add_argument("store"); sm.add_argument("state", choices=["on", "off"])
    n = sub.add_parser("need"); nsub = n.add_subparsers(dest="ncmd", required=True)
    na = nsub.add_parser("add"); na.add_argument("name")
    na.add_argument("--unit", default="pack", choices=["roll", "egg", "kg", "l", "piece", "pack"])
    na.add_argument("--category")
    nl = nsub.add_parser("link"); nl.add_argument("name"); nl.add_argument("eans", nargs="*")
    nl.add_argument("--offering", type=int, action="append", default=[])
    a = ap.parse_args()

    if a.cmd == "init": init()
    elif a.cmd == "seed": seed()
    elif a.cmd == "search": search(a.source, a.query)
    elif a.cmd == "match":
        if bool(a.ean) == bool(a.need): ap.error("match needs an EAN or --need, not both")
        match(a.source, a.ean, a.need, a.query, a.pick, a.qty)
    elif a.cmd == "fetch": fetch(a.source.split(",") if a.source else None)
    elif a.cmd == "matrix": matrix()
    elif a.cmd == "export": export()
    elif a.cmd == "need" and a.ncmd == "add": need_add(a.name, a.unit, a.category)
    elif a.cmd == "need" and a.ncmd == "link": need_link(a.name, a.eans, a.offering)
    elif a.cmd == "store" and a.scmd == "member": store_member(a.store, a.state == "on")

if __name__ == "__main__":
    main()
