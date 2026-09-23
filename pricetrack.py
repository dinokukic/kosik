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
    python pricetrack.py fetch [--source rohlik]       refresh store prices of matched items
    python pricetrack.py crawl                         weekly: every current Kupi flyer offer
    python pricetrack.py need add "4-ply TP" --unit roll
    python pricetrack.py match kupi 8593803421216      flyer offers at every chain (aggregator)
    python pricetrack.py need link "4-ply TP" 8584014858502 [--offering 12]
    python pricetrack.py store member kaufland off     ignore Kaufland Card prices
    python pricetrack.py matrix                        rows x stores, cheapest marked
    python pricetrack.py export                        writes dashboard/prices.json
    python pricetrack.py serve                         local dashboard + match screen :8765
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
                   url=excluded.url, source=excluded.source, active=1,
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
    if "packs" not in [r["name"] for r in c.execute("PRAGMA table_info(watch)")]:
        c.execute("ALTER TABLE watch ADD COLUMN packs TEXT")
    c.execute("DELETE FROM offering WHERE source='kupi'")  # Kupi lives in `deal` now
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
    for o in c.execute("SELECT * FROM offering WHERE source IN (%s)" % ",".join("?" * len(SOURCES)),
                       list(SOURCES)).fetchall():
        c.execute("INSERT OR IGNORE INTO watch(source,ref,product_id,need_id) VALUES (?,?,?,?)",
                  (o["source"], SOURCES[o["source"]]().ref_of(o["store_ref"]),
                   o["product_id"], o["need_id"]))
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
    aggregator = False
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
    aggregator = True  # prices go to the deal table (flyer layer), not to store offerings
    # Chains crawled weekly (/slevy/<slug>, ~18 products a page). Grocery + drugstore only.
    CRAWL_CHAINS = ["albert", "billa", "tesco", "kaufland", "lidl", "penny-market", "globus",
                    "makro", "kosik-cz", "flop", "flop-top", "cba", "coop", "hruska", "jip",
                    "eso-market", "ratio", "dm-drogerie", "teta-drogerie"]  # no Rossmann on Kupi
    # kupi chain slug -> our store id (unknown slugs become stores under Kupi's name).
    # NOTE: Kupi also carries Makro flyers; unverified whether those are bez DPH.
    CHAINS = {"penny-market": "penny", "teta-drogerie": "teta", "dm-drogerie": "dm"}

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
                     product=name, slug=slug, url=f"{self.BASE}/sleva/{slug}")
        return offers

    def crawl_chain(self, chain, max_pages=150):
        """Every current flyer offer of one chain, page by page (1.5 s apart)."""
        out = []
        for page in range(1, max_pages + 1):
            if page > 1: time.sleep(1.5)
            html = http_get(f"{self.BASE}/slevy/{chain}", {"page": page} if page > 1 else None,
                            json=False)
            rows, prods = self._rows(html), self._products(html)
            if not prods: break
            for pid, (slug, name) in prods.items():
                out += self._finish(slug, name, rows.get(pid, []))
        return out

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

class Makro:
    """VERIFIED live 2026-09-23 (logged-in inspection + anonymous calls). NO login needed:
    the anonymous search returns the NET (bez DPH) price per single pack, which x (1+VAT)
    equals the receipt exactly (butter 52.30 -> 58.58, Pampers 369 -> 446.49).
      search:  GET /searchdiscover/articlesearch/search?storeId=&query=<name|EAN>&rows=
               -> results {variantId: {price (net, the variant's smallest pack), offers}}
      detail:  GET /evaluate.article.v1/betty-variants?storeIds=&ids=<variantId>&ids=...
               -> per bundle (pack): description, gtins, bundleSize; article 'food' flag.
               Prices here are null unless logged in (then sellingPriceInfo has net/gross).
    A NAME search can price a case (40 x 250 g); matching is per bundle and prices are
    always re-read by the bundle's EAN. store_ref = that EAN. Store 00005 = Praha Černý Most."""
    aggregator = False
    BASE = "https://sortiment.makro.cz"
    STORE = os.environ.get("MAKRO_STORE", "00005")
    UAH = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
           "Accept": "application/json"}

    def _search(self, query, rows=12):
        return http_get(f"{self.BASE}/searchdiscover/articlesearch/search",
                        dict(storeId=self.STORE, language="cs-CZ", country="CZ", query=query,
                             rows=rows, page=1), self.UAH)

    def _variants(self, ids):
        if not ids: return {}
        params = [("storeIds", self.STORE), ("country", "CZ"), ("locale", "cs-CZ")] + \
                 [("ids", i) for i in ids]
        return http_get(f"{self.BASE}/evaluate.article.v1/betty-variants", params, self.UAH
                        ).get("result", {})

    def _bundles(self, ids, prices):
        """[(ean, description, pieces, food, net_price_or_None)] for the given variant ids."""
        out = []
        for art in self._variants(ids).values():
            for v in art.get("variants", {}).values():
                vid = (v.get("bettyVariantId") or {}).get("bettyVariantId")
                bundles = list((v.get("bundles") or {}).values())
                smallest = min((int(b.get("bundleSize") or 1) for b in bundles), default=1)
                for b in bundles:
                    eans = [g.get("number") for g in b.get("gtins") or [] if g.get("number")]
                    if not eans: continue
                    n = int(b.get("bundleSize") or 1)
                    # the search price belongs to the variant's smallest pack
                    net = prices.get(vid) if n == smallest else None
                    out.append((eans[0], b.get("description", ""), n, bool(art.get("food")), net))
        return out

    def _offer(self, ean, desc, net, food, vat=None):
        vat = vat if vat is not None else (12 if food else 21)
        return dict(store_id="makro", store_name="Makro", store_ref=ean, name=desc, amount="",
                    url=f"{self.BASE}/shop/search?q={ean}", price_incl=round(net * (1 + vat / 100), 2),
                    price_net=net, food=food, is_promo=False, valid_from=None, promo_until=None,
                    club=None, note=None, ref="")

    def search(self, query):
        res = self._search(query).get("results", {})
        prices = {k: v.get("price") for k, v in res.items()}
        cands = []
        for ean, desc, n, food, net in self._bundles(list(res)[:10], prices):
            # VAT depends on the item (Makro's 'food' flag is not the VAT class: diapers are
            # 'food'), so candidates show the net price; save_offers adds the item's VAT.
            offers = [dict(self._offer(ean, desc, net, food), price_incl=net, amount="bez DPH")] \
                if net is not None else []
            cands.append(dict(ref=ean, name=desc, url=f"{self.BASE}/shop/search?q={ean}", offers=offers))
        return cands

    def fetch(self, refs):
        """Net price per EAN (one search each, 1 s apart); VAT is applied in save_offers."""
        out = {}
        for i, ean in enumerate(refs):
            if i: time.sleep(1.0)
            res = self._search(ean, rows=3).get("results", {})
            if not res: continue
            vid, hit = next(iter(res.items()))
            b = [x for x in self._bundles([vid], {vid: hit.get("price")}) if x[0] == ean]
            if b and b[0][4] is not None:
                out[ean] = [self._offer(ean, b[0][1], b[0][4], b[0][3])]
        return out

    def ref_of(self, store_ref): return store_ref

# Heureka: not wired yet. Each needs the live-inspect treatment Rohlík and Kupi got
# before it gets a search()/fetch() adapter. Makro: needs MAKRO_COOKIE, shows bez DPH ->
# add product.vat_pct back.

SOURCES = {"rohlik": Rohlik, "makro": Makro, "kupi": Kupi}

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
        if o.get("price_net") is not None and product is not None and product["vat_pct"]:
            o = dict(o, price_incl=round(o["price_net"] * (1 + product["vat_pct"] / 100), 2))
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
        if product is not None:  # a live price supersedes a hand-entered one for that store
            c.execute("""UPDATE offering SET active=0 WHERE source='manual' AND store_id=?
                         AND product_id=?""", (o["store_id"], product["id"]))

def save_deals(c, offers, seen=None):
    """Upsert Kupi flyer offers into the deal table (the flyer layer)."""
    seen = seen or today()
    for o in offers:
        unit, qty = infer_unit(f"{o.get('product', '')} {o['amount']}", None)
        c.execute("INSERT OR IGNORE INTO store(id,name) VALUES (?,?)", (o["store_id"], o["store_name"]))
        c.execute("""INSERT INTO deal(ref,slug,product_name,store_id,store_name,pack_key,amount,unit,
                                      unit_qty,price_incl,valid_from,valid_to,club,note,
                                      first_seen,last_seen)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(ref) DO UPDATE SET price_incl=excluded.price_incl,
                       valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                       club=excluded.club, note=excluded.note, amount=excluded.amount,
                       unit=excluded.unit, unit_qty=excluded.unit_qty,
                       last_seen=excluded.last_seen""",
                  (o["ref"], o["slug"], o.get("product"), o["store_id"], o["store_name"],
                   o.get("key"), o["amount"], unit, qty if unit != "pack" else None,
                   o["price_incl"], o["valid_from"], o["promo_until"], o["club"], o["note"],
                   seen, seen))

def crawl(chains=None):
    """Weekly Kupi crawl: every current flyer offer of the tracked chains, then the product
    pages of watched items (catches chains outside the list). ~10-20 min, 1.5 s per page."""
    k, c, t0 = Kupi(), conn(), time.time()
    for chain in chains or Kupi.CRAWL_CHAINS:
        try:
            offers = k.crawl_chain(chain)
        except Exception as e:  # fail soft: this chain keeps last week's deals until they end
            print(f"  {chain:14} failed: {e}"); continue
        save_deals(c, offers)
        store_id = Kupi.CHAINS.get(chain, chain)
        c.execute("INSERT OR REPLACE INTO crawl_log(store_id,ts,deals) VALUES (?,?,?)",
                  (store_id, today(), len(offers)))
        c.commit()
        print(f"  {chain:14} {len(offers):5} offers  ({time.time() - t0:5.0f} s)")
        time.sleep(1.5)
    slugs = [r["ref"] for r in c.execute("SELECT DISTINCT ref FROM watch WHERE source='kupi'")]
    if slugs:
        got = k.fetch(slugs)
        save_deals(c, [o for offers in got.values() for o in offers])
    if not chains:  # a full run: deals from chains we don't crawl must be re-seen to count
        c.execute("INSERT OR REPLACE INTO crawl_log(store_id,ts,deals) VALUES ('*',?,NULL)", (today(),))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM deal WHERE last_seen=?", (today(),)).fetchone()[0]
    c.close()
    print(f"crawl done: {n} current offers, {len(slugs)} watched products refreshed, "
          f"{time.time() - t0:.0f} s")

def resolve_target(c, ean=None, need=None, product_id=None):
    """-> (product_row | None, need_row | None). One of product_id / ean / need."""
    if product_id or ean:
        product = c.execute("SELECT * FROM product WHERE id=?", (product_id,)).fetchone() \
            if product_id else c.execute("SELECT * FROM product WHERE ean=?", (ean,)).fetchone()
        if not product: raise ValueError(f"no product {product_id or ean}")
        return product, None
    need_row = c.execute("SELECT * FROM need WHERE name=?", (need,)).fetchone()
    if not need_row: raise ValueError(f"no need named {need!r} (run: need add)")
    return None, need_row

def retire_missing(c, src, ref, product=None, need=None, returned=()):
    """Offerings of this watch that the source no longer returns (flyer ended, product
    delisted) go inactive so their last price stops counting; a later fetch revives them."""
    col, val = ("product_id", product["id"]) if product else ("need_id", need["id"])
    s, keep = source(src), set(returned)
    for r in c.execute(f"SELECT id, store_ref FROM offering WHERE source=? AND {col}=? AND active=1",
                       (src, val)).fetchall():
        if s.ref_of(r["store_ref"]) == ref and r["store_ref"] not in keep:
            c.execute("UPDATE offering SET active=0 WHERE id=?", (r["id"],))

def link(src, ref, ean=None, need=None, qty=None, name=None, packs=None, product_id=None):
    """Cache a picked candidate as a watch, then pull its current offers: store offerings
    for a store source, flyer deals for Kupi. Returns the offers (may be none: a Kupi
    product with no flyer this week is still watched and picked up by later crawls)."""
    c = conn()
    try:
        product, need_row = resolve_target(c, ean, need, product_id)
        packs = ",".join(packs) if isinstance(packs, (list, tuple)) else packs or None
        c.execute("""INSERT INTO watch(source,ref,name,product_id,need_id,qty,packs)
                     VALUES (?,?,?,?,?,?,?)
                     ON CONFLICT DO UPDATE SET name=excluded.name, qty=excluded.qty,
                       packs=excluded.packs""",
                  (src, ref, name, product["id"] if product else None,
                   need_row["id"] if need_row else None, qty, packs))
        offers = source(src).fetch([ref]).get(ref, [])
        if source(src).aggregator:
            save_deals(c, offers)  # the flyer layer; pack filter applies in v_deal
            c.commit()
            keep = set(packs.split(",")) if packs else None
            return [o for o in offers if not keep or not o.get("key") or o["key"] in keep]
        save_offers(c, src, offers, product, need_row, qty)
        retire_missing(c, src, ref, product, need_row, [o["store_ref"] for o in offers])
        c.commit()
        return offers
    finally:
        c.close()

def unlink(src, ref, ean=None, need=None, product_id=None):
    """Undo a match: drop the watch and this ref's offerings (with prices) from the target."""
    c = conn()
    try:
        product, need_row = resolve_target(c, ean, need, product_id)
        col, val = ("product_id", product["id"]) if product else ("need_id", need_row["id"])
        c.execute(f"DELETE FROM watch WHERE source=? AND ref=? AND {col}=?", (src, ref, val))
        s = source(src)
        ids = [r["id"] for r in c.execute(f"SELECT id, store_ref FROM offering WHERE source=? AND {col}=?",
                                          (src, val)) if s.ref_of(r["store_ref"]) == ref]
        c.executemany("DELETE FROM offering WHERE id=?", [(i,) for i in ids])
        c.commit()
        return len(ids)
    finally:
        c.close()

def match(src, ean=None, need=None, query=None, pick=None, qty=None):
    """Add-a-product == match: search the source, you pick the right item, we cache its id.
    Link to a product (same EAN, `ean`) or, for a different brand/pack that satisfies the
    same need, only to the need (`need`). A Kupi pick brings every chain's flyer offer."""
    c = conn()
    try:
        product, need_row = resolve_target(c, ean, need)
    except ValueError as e:
        raise SystemExit(str(e))
    finally:
        c.close()
    target = product or need_row
    cands = search(src, query or default_query(target["name"]))
    if not cands: return
    if pick is None:
        raw = input(f"\nWhich is '{target['name']}'?  number, or blank to skip: ").strip()
        if not raw: return
        pick = int(raw)
    cd = cands[pick - 1]
    offers = link(src, cd["ref"], ean, need, qty, cd["name"])
    print(f"matched {target['name']} -> {src}:{cd['ref']} ({cd['name']}), "
          f"{len(offers)} offer(s): " + "; ".join(fmt_offer(o) for o in offers[:4]))

def default_query(name):
    """Receipt name -> search text the stores understand: drop Makro house-brand codes,
    sizes and counts; expand receipt abbreviations. Always editable in the UI."""
    q = re.sub(r"\b(MC|FL|ASC|NN|PAC)\b", " ", name)
    q = re.sub(r"\bTP\b", "toaletní papír", q)
    q = re.sub(r"\d+([.,]\d+)?\s*x\s*\d+\s*g\b|\d+([.,]\d+)?\s*(kg|g|ml|l|L|ks)\b|\d+-vrstv\w*"
               r"|\d+/\d+|\d+\s*%|\bvel\.\s*\d+|\bvaničk\w*", " ", q)
    return re.sub(r"\s+", " ", q).strip(" .,").lower()

def fetch(which):
    """Refresh store prices of everything matched. Kupi is refreshed by `crawl` instead."""
    c = conn()
    for src in which or [s for s, cls in SOURCES.items() if not cls.aggregator]:
        if SOURCES[src].aggregator:
            print(f"  {src}: flyer deals are refreshed by `crawl`"); continue
        watches = c.execute("SELECT * FROM watch WHERE source=?", (src,)).fetchall()
        if not watches:
            print(f"  {src}: nothing matched yet (run: match {src} <ean>)"); continue
        try:
            got = source(src).fetch(sorted({w["ref"] for w in watches}))
        except Exception as e:  # fail soft per source
            print(f"  [{src}] {e}"); continue
        for w in watches:
            product = c.execute("SELECT * FROM product WHERE id=?", (w["product_id"],)).fetchone() \
                if w["product_id"] else None
            need = c.execute("SELECT * FROM need WHERE id=?", (w["need_id"],)).fetchone() \
                if w["need_id"] else None
            keep = set(w["packs"].split(",")) if w["packs"] else None
            offers = [o for o in got.get(w["ref"]) or []
                      if not keep or not o.get("key") or o["key"] in keep]
            save_offers(c, src, offers, product, need, w["qty"])
            retire_missing(c, src, w["ref"], product, need, [o["store_ref"] for o in offers])
            if not offers:
                print(f"  {src:6} {(w['name'] or w['ref'])[:44]:44} (no current offers)")
            for o in offers:
                print(f"  {src:6} {o['name'][:44]:44} {fmt_offer(o)}")
        c.commit()
    c.close(); print("fetch done")

# ---------------------------------------------------------------- products (add / edit / delete)
PRODUCT_FIELDS = ("ean", "name", "category", "vat_pct", "unit", "unit_qty", "pack_note")

def product_add(name, category=None, vat_pct=None, ean=None, unit=None, unit_qty=None, pack_note=None):
    """New item to track. Unit + units-per-pack are inferred from the name unless given."""
    name = (name or "").strip()
    if not name: raise ValueError("name is required")
    u, q = infer_unit(name, category)
    c = conn()
    try:
        cur = c.execute("""INSERT INTO product(ean,name,category,vat_pct,unit,unit_qty,pack_note)
                           VALUES (?,?,?,?,?,?,?)""",
                        ((ean or "").strip() or None, name, category or None,
                         int(vat_pct) if vat_pct not in (None, "") else None,
                         unit or u, float(unit_qty) if unit_qty not in (None, "") else q, pack_note))
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"an item with EAN {ean} already exists")
    finally:
        c.close()

def product_update(pid, **fields):
    sets = {k: v for k, v in fields.items() if k in PRODUCT_FIELDS}
    if "ean" in sets: sets["ean"] = (sets["ean"] or "").strip() or None
    if "unit_qty" in sets: sets["unit_qty"] = float(sets["unit_qty"])
    if "vat_pct" in sets: sets["vat_pct"] = int(sets["vat_pct"]) if sets["vat_pct"] not in (None, "") else None
    if not sets: return
    c = conn()
    try:
        c.execute(f"UPDATE product SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                  (*sets.values(), pid))
        c.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"an item with EAN {sets.get('ean')} already exists")
    finally:
        c.close()

def product_delete(pid):
    """Remove an item with everything matched to it (offerings + their prices, watches)."""
    c = conn()
    c.execute("DELETE FROM offering WHERE product_id=?", (pid,))
    c.execute("DELETE FROM watch WHERE product_id=?", (pid,))
    c.execute("DELETE FROM product WHERE id=?", (pid,))
    c.commit(); c.close()

def lookup(q):
    """EAN (or name) -> what the barcode-aware stores call it, to prefill a new item."""
    return [dict(source="makro", ean=cd["ref"], name=cd["name"]) for cd in Makro().search(q)[:8]]

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
DEAL_FIELDS = ("store_id", "store_name", "product_name", "amount", "price_incl", "unit_price",
               "valid_from", "valid_to", "club", "note", "slug")

def matrix_rows():
    """Store cells from direct sources, plus each row's best Kupi flyer deal. The cheapest
    mark goes to whichever is lower per unit: a store cell or the flyer deal."""
    c = conn()
    cells = [dict(r) for r in c.execute("SELECT * FROM v_matrix ORDER BY category, row_name")]
    deals = [dict(r) for r in c.execute("SELECT * FROM v_row_deal")]
    stores = [dict(r) for r in c.execute(
        "SELECT id, name FROM store WHERE id IN (SELECT DISTINCT store_id FROM v_matrix) "
        "ORDER BY id")]
    c.close()
    rows = {}
    def row(key, name, category, unit):
        return rows.setdefault(key, dict(key=key, name=name, category=category, unit=unit,
                                         cells={}, deal=None))
    for cell in cells:
        r = row(cell["row_key"], cell["row_name"], cell["category"], cell["unit"])
        r["cells"][cell["store_id"]] = {k: cell[k] for k in (
            "offering_id", "offering_name", "source", "price_incl", "unit_qty", "unit_price",
            "is_promo", "promo_until", "club", "note", "ts", "url")} | {"is_cheapest": bool(cell["is_cheapest"])}
    for d in deals:
        r = row(d["row_key"], d["row_name"], d["category"], d["row_unit"])
        r["deal"] = {k: d[k] for k in DEAL_FIELDS} | {
            "url": f"{Kupi.BASE}/sleva/{d['slug']}", "is_cheapest": False}
        best = min([x["unit_price"] for x in r["cells"].values()] or [float("inf")])
        if d["unit_price"] <= best:
            r["deal"]["is_cheapest"] = True
            if d["unit_price"] < best:
                for x in r["cells"].values(): x["is_cheapest"] = False
    return stores, sorted(rows.values(), key=lambda r: (r["category"] or "", r["name"]))

def matrix():
    stores, rows = matrix_rows()
    ids = [s["id"] for s in stores]
    print("\n%-34s %-6s %-22s" % ("ROW", "PER", "FLYER (kupi.cz)") + "".join(f"{s[:8]:>9}" for s in ids))
    print("-" * (64 + 9 * len(ids)))
    for r in rows:
        d = r["deal"]
        flyer = f"{d['unit_price']:.2f}{'*' if d['is_cheapest'] else ' '} {d['store_id'][:12]}" if d else "·"
        line = "%-34s %-6s %-22s" % (r["name"][:34], r["unit"], flyer)
        for s in ids:
            cell = r["cells"].get(s)
            if not cell: line += f"{'·':>9}"; continue
            mark = "*" if cell["is_cheapest"] else " "
            line += f"{cell['unit_price']:8.2f}{mark}"
        print(line)
    print("\n* = cheapest per unit in the row. Store prices from the stores; FLYER = best current"
          "\n    flyer deal from kupi.cz. Prices incl. VAT.\n")

def export():
    stores, rows = matrix_rows()
    EXPORT.parent.mkdir(exist_ok=True)
    EXPORT.write_text(json.dumps(dict(
        generated=dt.datetime.now().isoformat(timespec="minutes"), stores=stores, rows=rows),
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"export ok -> {EXPORT} ({len(rows)} rows x {len(stores)} stores)")

# ---------------------------------------------------------------- local server
def products_status():
    """Every product with what it's matched to, per source, and today's offers."""
    c = conn()
    prods = [dict(r) for r in c.execute("""SELECT p.*, n.name AS need_name FROM product p
                                           LEFT JOIN need n ON n.id = p.need_id
                                           ORDER BY p.category, p.name""")]
    watches = [dict(r) for r in c.execute("SELECT * FROM watch WHERE product_id IS NOT NULL")]
    offers = [dict(r) for r in c.execute("""SELECT product_id, source, store_ref, store_id,
                                                   offering_name, price_incl, unit_price, unit,
                                                   promo_until, club
                                            FROM v_offer WHERE product_id IS NOT NULL""")]
    stores = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM store")}
    deals = [dict(r) for r in c.execute("""SELECT product_id, slug, store_id, store_name, amount,
                                                  price_incl, valid_from, valid_to AS promo_until,
                                                  club, note FROM v_deal
                                           WHERE product_id IS NOT NULL ORDER BY price_incl""")]
    c.close()
    for p in prods:
        p["query"] = default_query(p["name"])
        p["matches"] = []
        for w in (w for w in watches if w["product_id"] == p["id"]):
            s = source(w["source"])
            if s.aggregator:
                p["matches"].append(dict(source=w["source"], ref=w["ref"], name=w["name"] or w["ref"],
                    packs=w["packs"], offers=[d for d in deals
                                              if d["product_id"] == p["id"] and d["slug"] == w["ref"]]))
                continue
            p["matches"].append(dict(source=w["source"], ref=w["ref"], name=w["name"] or w["ref"],
                offers=[dict(o, store_name=stores.get(o["store_id"], o["store_id"]))
                        for o in offers if o["product_id"] == p["id"] and o["source"] == w["source"]
                        and s.ref_of(o["store_ref"]) == w["ref"] and o["price_incl"] is not None]))
        p["manual"] = [dict(o, store_name=stores.get(o["store_id"], o["store_id"]))
                       for o in offers if o["product_id"] == p["id"] and o["source"] == "manual"
                       and o["price_incl"] is not None]
    return prods

def serve(port):
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs
    import functools, traceback
    import threading, io, contextlib
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    crawl_state = {"thread": None, "log": io.StringIO()}

    def start_crawl():
        if crawl_state["thread"] and crawl_state["thread"].is_alive(): return False
        crawl_state["log"] = log = io.StringIO()
        def work():
            with contextlib.redirect_stdout(log):
                try: crawl()
                except Exception as e: print(f"crawl failed: {e}")
        crawl_state["thread"] = threading.Thread(target=work, daemon=True)
        crawl_state["thread"].start()
        return True

    class Handler(SimpleHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _guard(self):
            # local only: refuse other Host headers (DNS rebinding) and non-JSON writes
            # (a cross-site form can't send application/json without a CORS preflight)
            if self.headers.get("Host") not in allowed_hosts:
                self._json({"error": "bad host"}, 403); return False
            return True

        def log_message(self, fmt, *args):
            if self.path.startswith("/api/"): super().log_message(fmt, *args)

        def do_GET(self):
            if not self._guard(): return
            u = urlparse(self.path)
            if not u.path.startswith("/api/"): return super().do_GET()
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/matrix":
                    stores, rows = matrix_rows()
                    return self._json(dict(generated=dt.datetime.now().isoformat(timespec="minutes"),
                                           stores=stores, rows=rows))
                if u.path == "/api/products":
                    return self._json(dict(products=products_status(), sources=list(SOURCES)))
                if u.path == "/api/crawl":
                    t = crawl_state["thread"]
                    return self._json(dict(running=bool(t and t.is_alive()),
                                           log=crawl_state["log"].getvalue()[-2000:]))
                if u.path == "/api/lookup":
                    return self._json(dict(results=lookup(q["q"])))
                if u.path == "/api/search":
                    return self._json(dict(candidates=source(q["source"]).search(q["q"])))
                self._json({"error": "not found"}, 404)
            except Exception as e:
                traceback.print_exc(); self._json({"error": str(e)}, 500)

        def do_POST(self):
            if not self._guard(): return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "json only"}, 415)
            try:
                a = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                u = urlparse(self.path).path
                if u == "/api/link":
                    offers = link(a["source"], a["ref"], a.get("ean"), a.get("need"),
                                  a.get("qty"), a.get("name"), a.get("packs"), a.get("product_id"))
                    return self._json(dict(ok=True, offers=len(offers)))
                if u == "/api/unlink":
                    return self._json(dict(ok=True, removed=unlink(a["source"], a["ref"],
                                           a.get("ean"), a.get("need"), a.get("product_id"))))
                if u == "/api/product":
                    pid = product_add(**{k: a.get(k) for k in PRODUCT_FIELDS})
                    return self._json(dict(ok=True, id=pid))
                if u == "/api/product/update":
                    product_update(a["id"], **{k: a[k] for k in PRODUCT_FIELDS if k in a})
                    return self._json(dict(ok=True))
                if u == "/api/product/delete":
                    product_delete(a["id"])
                    return self._json(dict(ok=True))
                if u == "/api/crawl":
                    return self._json(dict(ok=True, started=start_crawl()))
                if u == "/api/fetch":
                    fetch(a.get("sources")); export()
                    return self._json(dict(ok=True))
                self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                traceback.print_exc(); self._json({"error": str(e)}, 500)

    handler = functools.partial(Handler, directory=str(EXPORT.parent))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Košík on http://localhost:{port}  (matrix)  ·  http://localhost:{port}/match.html  (match)")
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass

# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="Košík price tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "seed", "matrix", "export"): sub.add_parser(name)
    sv = sub.add_parser("serve", help="local dashboard + match screen")
    sv.add_argument("--port", type=int, default=8765)
    s = sub.add_parser("search"); s.add_argument("source", choices=list(SOURCES))
    s.add_argument("query")
    m = sub.add_parser("match"); m.add_argument("source", choices=list(SOURCES))
    m.add_argument("ean", nargs="?", help="link to this product (same item)")
    m.add_argument("--need", help="link only to this need (different brand/pack)")
    m.add_argument("--query", help="search text (default: product/need name)")
    m.add_argument("--qty", type=float, help="units per pack, if it can't be read from the name")
    m.add_argument("--pick", type=int, help="candidate number (skip the prompt)")
    cr = sub.add_parser("crawl", help="weekly Kupi flyer crawl (~10-20 min)")
    cr.add_argument("--chains", help="comma list of kupi chain slugs (default: Kupi.CRAWL_CHAINS)")
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
    elif a.cmd == "crawl": crawl(a.chains.split(",") if a.chains else None)
    elif a.cmd == "fetch": fetch(a.source.split(",") if a.source else None)
    elif a.cmd == "matrix": matrix()
    elif a.cmd == "export": export()
    elif a.cmd == "serve": serve(a.port)
    elif a.cmd == "need" and a.ncmd == "add": need_add(a.name, a.unit, a.category)
    elif a.cmd == "need" and a.ncmd == "link": need_link(a.name, a.eans, a.offering)
    elif a.cmd == "store" and a.scmd == "member": store_member(a.store, a.state == "on")

if __name__ == "__main__":
    main()
