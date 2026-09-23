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
import argparse, csv, json, os, re, sqlite3, sys, time, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "prices.db"
SCHEMA = ROOT / "bunny" / "schema.sql"
WATCHLIST = ROOT / "watchlist.csv"
EXPORT = ROOT / "dashboard" / "prices.json"
UA = "personal-pricetrack/1.0"

# .env (gitignored) -> environment: TESCO_XAPI_KEY, ROHLIK_COOKIE, MAKRO_STORE, Bunny creds
if (ROOT / ".env").exists():
    for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        _k, _, _v = _line.partition("=")
        if _k.strip() and not _k.startswith("#") and _v: os.environ.setdefault(_k.strip(), _v.strip())

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

# ---------------------------------------------------------------- database
# Bunny Database (libSQL over HTTP) is the database; prices.db is only a local fallback
# (PRICETRACK_DB=local). BunnyConn mimics the bit of sqlite3's connection API this file
# uses, so every function works on either backend.

class Row:
    """sqlite3.Row look-alike: row["col"], row[0], dict(row), row.keys()."""
    __slots__ = ("_cols", "_vals")
    def __init__(self, cols, vals): self._cols, self._vals = cols, vals
    def keys(self): return list(self._cols)
    def __getitem__(self, k): return self._vals[k] if isinstance(k, int) else self._vals[self._cols.index(k)]
    def __iter__(self): return iter(self._vals)
    def __len__(self): return len(self._vals)
    def __repr__(self): return f"Row({dict(zip(self._cols, self._vals))})"

class Cursor:
    def __init__(self, rows=(), rowcount=0, lastrowid=None):
        self._rows, self.rowcount, self.lastrowid = list(rows), rowcount, lastrowid
    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return list(self._rows)
    def __iter__(self): return iter(self._rows)

class BunnyConn:
    """Stateless: every call is one POST to /v2/pipeline (keep-alive session per thread).
    Multi-statement writes go as one `batch` inside BEGIN/COMMIT. Foreign keys are on."""
    CHUNK = 300  # statements per batch request
    _local = __import__("threading").local()

    def __init__(self):
        self.url = os.environ["BUNNY_DATABASE_URL"].replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self.token = os.environ["BUNNY_DATABASE_AUTH_TOKEN"]

    def _session(self):
        import requests
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = requests.Session()
            s.headers.update({"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                              "User-Agent": UA})
        return s

    @staticmethod
    def _arg(v):
        if v is None: return {"type": "null"}
        if isinstance(v, bool): return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int): return {"type": "integer", "value": str(v)}
        if isinstance(v, float): return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _val(v):
        t = v["type"]
        return None if t == "null" else int(v["value"]) if t == "integer" else v.get("value")

    @staticmethod
    def _raise(err):
        msg = (err or {}).get("message", "database error")
        if "constraint" in msg.lower(): raise sqlite3.IntegrityError(msg)
        raise sqlite3.OperationalError(msg)

    def _send(self, requests_, attempts=3):
        """One pipeline request. Transient failures (5xx, 429, dropped connection) are retried
        with back-off; each call is a self-contained pipeline, so a retry can't half-apply."""
        import requests as rq
        body = {"baton": None, "requests": [*requests_, {"type": "close"}]}
        for i in range(attempts):
            try:
                r = self._session().post(self.url, json=body, timeout=60)
            except (rq.ConnectionError, rq.Timeout) as e:
                if i == attempts - 1: raise sqlite3.OperationalError(f"Bunny DB unreachable: {e}")
                time.sleep(2 ** i); continue
            if (r.status_code >= 500 or r.status_code == 429) and i < attempts - 1:
                time.sleep(2 ** i); continue
            break
        if r.status_code != 200: raise sqlite3.OperationalError(f"Bunny DB HTTP {r.status_code}: {r.text[:200]}")
        res = r.json()["results"][0]
        if res.get("type") == "error": self._raise(res.get("error"))
        return res["response"]

    def _stmt(self, sql, params=()):
        return {"sql": sql, "args": [self._arg(p) for p in params], "want_rows": True}

    def _cursor(self, result):
        cols = [c.get("name") for c in result.get("cols", [])]
        rows = [Row(cols, [self._val(v) for v in row]) for row in result.get("rows", [])]
        lid = result.get("last_insert_rowid")
        return Cursor(rows, result.get("affected_row_count", 0), int(lid) if lid is not None else None)

    def execute(self, sql, params=()):
        return self._cursor(self._send([{"type": "execute", "stmt": self._stmt(sql, params)}])["result"])

    def batch(self, stmts):
        """[(sql, params)] in one transaction per CHUNK statements."""
        stmts = list(stmts)
        for i in range(0, len(stmts), self.CHUNK):
            part = stmts[i:i + self.CHUNK]
            ok = lambda step: {"type": "ok", "step": step}
            steps = [{"stmt": {"sql": "BEGIN", "args": [], "want_rows": False}}]
            steps += [{"stmt": self._stmt(q, p), "condition": ok(j)} for j, (q, p) in enumerate(part)]
            steps += [{"stmt": {"sql": "COMMIT", "args": [], "want_rows": False}, "condition": ok(len(part))},
                      {"stmt": {"sql": "ROLLBACK", "args": [], "want_rows": False},
                       "condition": {"type": "not", "cond": ok(len(part) + 1)}}]
            res = self._send([{"type": "batch", "batch": {"steps": steps}}])["result"]
            errs = [e for e in res.get("step_errors", []) if e]
            if errs: self._raise(errs[0])

    def executemany(self, sql, seq): self.batch((sql, p) for p in seq)

    def executescript(self, script):
        self._send([{"type": "sequence", "sql": script}])

    def commit(self): pass
    def close(self): pass

def use_bunny():
    if os.environ.get("PRICETRACK_DB", "").lower() == "local": return False
    return bool(os.environ.get("BUNNY_DATABASE_URL") and os.environ.get("BUNNY_DATABASE_AUTH_TOKEN"))

def conn():
    if use_bunny(): return BunnyConn()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON"); return c


def today(): return dt.date.today().isoformat()

# ---------------------------------------------------------------- units
def infer_unit(name, category):
    """Price-per-obvious-unit, kept simple: kg for weights, l for volumes,
    roll/egg/piece for counts. Returns (unit, unit_qty); ('pack', 1) if unsure."""
    n = name.lower().replace(",", ".").replace("×", "x")
    if m := re.search(r"(\d+)\s*(?:ks|pcs|kusů|plenek|rolí|role)\b", n):  # a count beats a size (bags: 60L 15ks)
        q = int(m[1])
        if re.search(r"\btp\b|toalet|utěrk|rolí", n): return "roll", q
        if "vejce" in n: return "egg", q
        return "piece", q
    if m := re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", n):  # multipack: 6 x 1,5 l
        k = int(m[1]) * float(m[2])
        return {"kg": ("kg", k), "g": ("kg", k / 1000), "ml": ("l", k / 1000), "l": ("l", k)}[m[3]]
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
                   unit_qty=excluded.unit_qty""",  # recomputed on every save; a forced qty comes via watch.qty
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

# ---------------------------------------------------------------- one-time move to Bunny
MIGRATE_TABLES = ["store", "need", "product", "watch", "offering", "price", "deal", "crawl_log"]

def migrate_to_bunny():
    """Copy the local prices.db into Bunny Database, ids preserved. Bunny's own rows (only the
    original seed so far) are replaced. prices.db is left untouched as a fallback."""
    if not use_bunny(): raise SystemExit("Bunny creds missing in .env (or PRICETRACK_DB=local)")
    src = sqlite3.connect(DB); src.row_factory = sqlite3.Row
    dst = BunnyConn()
    dst.executescript(SCHEMA.read_text(encoding="utf-8"))
    if "packs" not in [r["name"] for r in dst.execute("PRAGMA table_info(watch)")]:
        dst.execute("ALTER TABLE watch ADD COLUMN packs TEXT")
    dst.batch([(f"DELETE FROM {t}", ()) for t in reversed(MIGRATE_TABLES)])
    for t in MIGRATE_TABLES:
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if not rows: print(f"  {t:10} 0"); continue
        cols = rows[0].keys()
        t0 = time.time()
        dst.executemany(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [tuple(r) for r in rows])
        print(f"  {t:10} {len(rows):6}  ({time.time() - t0:.1f} s)")
    bad = [(t, a, b) for t in MIGRATE_TABLES
           for a, b in [(src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0],
                         dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])] if a != b]
    print("migrate ok: row counts match" if not bad else f"MISMATCH: {bad}")

# ---------------------------------------------------------------- init / seed
def init():
    c = conn(); c.executescript(SCHEMA.read_text(encoding="utf-8"))
    if "packs" not in [r["name"] for r in c.execute("PRAGMA table_info(watch)")]:
        c.execute("ALTER TABLE watch ADD COLUMN packs TEXT")
    if "search_query" not in [r["name"] for r in c.execute("PRAGMA table_info(product)")]:
        c.execute("ALTER TABLE product ADD COLUMN search_query TEXT")
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
        ids = self._get("/services/frontend-service/autocomplete",   # limit: default is only 8
                        {"search": query, "referer": "whisperer", "companyId": 1, "limit": n}
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

    def search(self, query, n=None):  # one results page (~20 products); n is ignored
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
    ean_search = True  # search accepts a barcode and returns that exact item
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
        out = {}
        for i in range(0, len(ids), 20):  # the endpoint rejects long id lists (60 -> HTTP 400)
            params = [("storeIds", self.STORE), ("country", "CZ"), ("locale", "cs-CZ")] + \
                     [("ids", x) for x in ids[i:i + 20]]
            out.update(http_get(f"{self.BASE}/evaluate.article.v1/betty-variants", params, self.UAH
                                ).get("result", {}))
        return out

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

    def search(self, query, n=12):
        res = self._search(query, rows=n).get("results", {})
        prices = {k: v.get("price") for k, v in res.items()}
        cands = []
        for ean, desc, _, food, net in self._bundles(list(res)[:n], prices):
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

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

def store_offer(store_id, store_name, ref, name, amount, url, price, regular=None,
                until=None, note=None, ean=None):
    """Offer dict for a direct store source. Promo = paying less than the regular price."""
    promo = regular is not None and price is not None and price < regular - 0.001
    return dict(store_id=store_id, store_name=store_name, store_ref=str(ref), name=name,
                amount=amount or "", url=url, price_incl=float(price), is_promo=promo,
                valid_from=None, promo_until=until if promo else None, club=None,
                note=note if promo else None, ref="", ean=str(ean) if ean else None)

def pack_size(title):
    """Pack size from a product title: a piece count wins ('72 Plenek', '16 ks'), then
    'N x size', then weight/volume; ranges like '2kg-5kg' / '2–5 kg' (diaper sizes) are skipped."""
    t = re.sub(r"\d+(?:[.,]\d+)?\s*(?:kg|g)?\s*[-–]\s*\d+(?:[.,]\d+)?\s*(?:kg|g)\b", " ", title or "", flags=re.I)
    for pat in (r"(\d+)\s*(?:ks|kusů|plenek|rolí|role)\b", r"(\d+\s*x\s*\d+(?:[.,]\d+)?\s*(?:kg|g|ml|l))\b",
                r"(\d+(?:[.,]\d+)?\s*(?:kg|g|ml|l))\b"):
        m = re.search(pat, t, re.I)
        if m: return m[0].strip()
    return ""

def is_ean(q): return bool(re.fullmatch(r"\d{8,14}", (q or "").strip()))

class Globus:
    ean_search = True  # search accepts a barcode and returns that exact item
    """VERIFIED 2026-09-23. globusonline.cz Shopsys GraphQL, no login. Search by name or EAN
    (fuzzy: an EAN query can return a neighbouring EAN, so exact-match it). Batch by catalogue
    number. Promo = priority SPECIAL_PRICE with globusOriginalPrice. robots: crawl-delay 3."""
    aggregator = False
    URL = "https://globusonline.cz/graphql/"
    FIELDS = ("uuid name catalogNumber ean sellUnitSizeText slug "
              "price{priceWithVat globusOriginalPrice globusDiscountPercentage priority}")

    def _q(self, query, variables):
        import requests
        r = requests.post(self.URL, json=dict(query=query, variables=variables), timeout=20,
                          headers={"User-Agent": BROWSER_UA, "Content-Type": "application/json"})
        r.raise_for_status()
        d = r.json()
        if d.get("errors"): raise RuntimeError(d["errors"][0].get("message"))
        return d["data"]

    def _offer(self, n):
        p = n.get("price") or {}
        orig = p.get("globusOriginalPrice")
        disc = p.get("globusDiscountPercentage")
        return store_offer("globus", "Globus", n["catalogNumber"], n["name"], n.get("sellUnitSizeText"),
                           f"https://globusonline.cz/{n['slug'].lstrip('/')}" if n.get("slug") else
                           "https://globusonline.cz", float(p["priceWithVat"]),
                           float(orig) if orig else None, note=f"−{float(disc):.0f} %" if disc else None,
                           ean=n.get("ean"))

    def search(self, query, n=12):
        d = self._q("query S($q:String!,$n:Int!){productsSearch(searchInput:{search:$q,isAutocomplete:false,"
                    "userIdentifier:\"00000000-0000-4000-8000-000000000000\"},first:$n){edges{node{%s}}}}"
                    % self.FIELDS, dict(q=query, n=n))
        nodes = [e["node"] for e in d["productsSearch"]["edges"]]
        if is_ean(query): nodes = [x for x in nodes if x.get("ean") == query.strip()]
        return [dict(ref=o["store_ref"], name=o["name"], url=o["url"], offers=[o])
                for o in map(self._offer, nodes)]

    def fetch(self, refs):
        out, refs = {}, list(refs)
        for i in range(0, len(refs), 50):
            if i: time.sleep(3)
            d = self._q("query($c:[String!]!){productsByCatnums(catnums:$c){%s}}" % self.FIELDS,
                        dict(c=refs[i:i + 50]))
            for n in d["productsByCatnums"] or []:
                out[n["catalogNumber"]] = [self._offer(n)]
        return out

    def ref_of(self, store_ref): return store_ref

class Kosik:
    ean_search = True  # search accepts a barcode and returns that exact item
    """VERIFIED 2026-09-23. kosik.cz front API, no login. Search by name or EAN (suggest/v2;
    EAN isn't in the JSON). Price per product id. price < recommendedPrice = promo, end date
    in actionLabel ('Akce platí do 29. 9.'). A single-item EAN can map to a multipack."""
    aggregator = False
    BASE = "https://www.kosik.cz"

    def _get(self, path, params=None):
        return http_get(self.BASE + path, params, {"User-Agent": BROWSER_UA, "Accept": "application/json"})

    def _offer(self, it):
        q = it.get("productQuantity") or {}
        amount = f"{q['value']:g} {q['unit']}" if q.get("value") else ""
        until = Kupi._dates(it.get("actionLabel") or "")[1]
        return store_offer("kosik-cz", "Košík.cz", it["id"], it["name"], amount,
                           self.BASE + (it.get("url") or f"/p{it['id']}"), it["price"],
                           it.get("recommendedPrice"), until,
                           f"−{it['percentageDiscount']} %" if it.get("percentageDiscount") else None)

    def search(self, query, n=12):
        items = (self._get("/api/front/suggest/v2", dict(query=query, limit=n)).get("products") or {}
                 ).get("items") or []
        return [dict(ref=o["store_ref"], name=o["name"], url=o["url"], offers=[o])
                for o in map(self._offer, items) if o]

    def fetch(self, refs):
        out = {}
        for i, ref in enumerate(refs):
            if i: time.sleep(1.0)
            try:
                it = self._get(f"/api/front/product/{ref}")["product"]
            except Exception as e:
                print(f"  [kosik] {ref}: {e}"); continue
            out[str(ref)] = [self._offer(it)]
        return out

    def ref_of(self, store_ref): return store_ref

class Dm:
    ean_search = True  # search accepts a barcode and returns that exact item
    """VERIFIED 2026-09-23. dm.cz, no login, one national price incl. VAT. Search by name or
    EAN (product-search, rate-limited: ~5 quick calls -> 429 for ~20 s). Batch price by DAN
    (dm's product id). Promo = price.previous (strike-through) — inferred, not yet seen."""
    aggregator = False
    SEARCH = "https://product-search.services.dmtech.com/cz/search/crawl"
    TILES = "https://products.dm.de/product/products/tiles/CZ/dans/"

    @staticmethod
    def _num(s):
        m = re.search(r"[\d\s]+,\d+|\d+", (s or "").replace("\xa0", " "))
        return float(m[0].replace(" ", "").replace(",", ".")) if m else None

    def _offer(self, p):
        pr = ((p.get("tileData") or p).get("price") or {})
        cur = self._num(((pr.get("price") or {}).get("current") or {}).get("value"))
        prev = self._num(((pr.get("price") or {}).get("previous") or {}).get("value"))
        if cur is None: cur = (p.get("trackingData") or (p.get("tileData") or {}).get("trackingData") or {}).get("price")
        if cur is None: return None
        infos = pr.get("tileInfos") or pr.get("infos") or []
        amount = infos[0].split("(")[0].strip() if infos else ""
        title = p.get("title")
        title = title.get("tileHeadline") if isinstance(title, dict) else title
        name = f"{p.get('brandName', '')} {title or ''}".strip()
        self_url = (p.get("tileData") or {}).get("self") or p.get("self") or f"/p/d/{p['dan']}"
        return store_offer("dm", "dm", p["dan"], name, amount, "https://www.dm.cz" + self_url,
                           cur, prev, ean=p.get("gtin"))

    def search(self, query, n=10):
        import requests
        for attempt in range(2):
            r = requests.get(self.SEARCH, params=dict(query=query, searchType="product", pageSize=n),
                             headers={"User-Agent": BROWSER_UA}, timeout=20)
            if r.status_code != 429: break
            time.sleep(20)  # dm's search rate limit clears after ~20 s
        r.raise_for_status()
        prods = r.json().get("products") or []
        if is_ean(query): prods = [p for p in prods if str(p.get("gtin")) == query.strip()] or prods
        return [dict(ref=o["store_ref"], name=o["name"], url=o["url"], offers=[o])
                for o in map(self._offer, prods) if o]

    def fetch(self, refs):
        out, refs = {}, list(refs)
        for i in range(0, len(refs), 30):
            if i: time.sleep(2)
            d = http_get(self.TILES + ",".join(refs[i:i + 30]), None, {"User-Agent": BROWSER_UA})
            for dan, p in (d.get("products") or {}).items():
                o = self._offer(dict(p, dan=p.get("dan", dan)))
                if o: out[str(dan)] = [o]
        return out

    def ref_of(self, store_ref): return store_ref

def extra_price(o, price, ref, club=None, note=None, promo=False, until=None):
    """Another price for the same listing (card / conditional promo): same offering, own row."""
    return dict(o, price_incl=float(price), ref=ref, club=club, note=note, is_promo=promo,
                promo_until=until if promo else None)

class Tesco:
    """VERIFIED 2026-09-23 via xapi.tesco.com GraphQL (the website is behind Akamai; the API
    isn't). Needs the public web-app key in .env TESCO_XAPI_KEY. No EAN search, but every
    product carries its gtin -> barcode matches search by name and keep the exact gtin.
    price.actual = current incl. normal promo; Clubcard price only as text in a promotion
    with attributes CLUBCARD_PRICING ("119,90 Kč ... s Clubcard") -> parsed into a club row."""
    aggregator = False
    ean_via_name = True
    URL = "https://xapi.tesco.com/"
    NODE = ("id gtin title isForSale price{actual unitPrice unitOfMeasure} "
            "promotions{startDate endDate description attributes price{beforeDiscount afterDiscount}}")

    def _post(self, ops):
        import requests
        key = os.environ.get("TESCO_XAPI_KEY")
        if not key: raise RuntimeError("TESCO_XAPI_KEY missing in .env (see docs/store-apis.md)")
        r = requests.post(self.URL, json=ops, timeout=20, headers={
            "User-Agent": BROWSER_UA, "Content-Type": "application/json", "x-apikey": key,
            "language": "cs-CZ", "region": "CZ"})
        r.raise_for_status()
        return r.json()

    def _offers(self, n):
        if not n or not n.get("price") or n["price"].get("actual") is None: return []
        regular, until, club = None, None, None
        for pr in n.get("promotions") or []:
            if "CLUBCARD_PRICING" in (pr.get("attributes") or []):
                m = re.search(r"(\d+(?:[ \xa0]\d{3})*,\d{2})\s*Kč", pr.get("description") or "")
                if m: club = (float(m[1].replace(" ", "").replace("\xa0", "").replace(",", ".")),
                              (pr.get("endDate") or "")[:10] or None)
            elif (pr.get("price") or {}).get("beforeDiscount"):
                regular = pr["price"]["beforeDiscount"]; until = (pr.get("endDate") or "")[:10] or None
        gtin = (n.get("gtin") or "").lstrip("0") or None
        o = store_offer("tesco", "Tesco", n["id"], n["title"], pack_size(n["title"]),
                        f"https://nakup.itesco.cz/groceries/cs-CZ/products/{n['id']}",
                        n["price"]["actual"], regular, until, ean=gtin)
        out = [o]
        if club and club[0] < o["price_incl"]:
            out.append(extra_price(o, club[0], "clubcard", club="Tesco Clubcard", promo=True, until=club[1]))
        return out

    def search(self, query, n=12):
        d = self._post([{"operationName": "S", "variables": {"q": query, "n": n}, "query":
            "query S($q:String!,$n:Int){search(query:$q,page:1,count:$n){results{node{... on ProductInterface{%s}}}}}"
            % self.NODE}])
        nodes = [r["node"] for r in (d[0]["data"]["search"] or {}).get("results") or []]
        return [dict(ref=offs[0]["store_ref"], name=offs[0]["name"], url=offs[0]["url"], offers=offs)
                for offs in map(self._offers, nodes) if offs]

    def fetch(self, refs):
        out, refs = {}, list(refs)
        for i in range(0, len(refs), 20):
            if i: time.sleep(1.5)
            ops = [{"operationName": f"P{j}", "variables": {"t": t}, "query":
                    "query P%d($t:String){product(tpnc:$t){%s}}" % (j, self.NODE)}
                   for j, t in enumerate(refs[i:i + 20])]
            for res in self._post(ops):
                offs = self._offers((res.get("data") or {}).get("product"))
                if offs: out[offs[0]["store_ref"]] = offs
        return out

    def ref_of(self, store_ref): return store_ref

class Billa:
    """VERIFIED 2026-09-23. billa.cz catalogue API (checkout goes via Wolt/foodora, prices
    are Billa's own). No login, no EAN anywhere -> match by name. Amounts in haléře.
    During a promo regular.value is the PROMO price and standard.value the shelf price."""
    aggregator = False
    BASE = "https://www.billa.cz"

    def _offer(self, p):
        pr = p.get("price") or {}
        cur = (pr.get("regular") or {}).get("value")
        if cur is None: return None
        std = (pr.get("standard") or {}).get("value") or pr.get("crossed")
        amount = f"{p.get('amount', '')} {p.get('volumeLabelShort', '')}".strip()
        return store_offer("billa", "Billa", p["sku"], p["name"], amount,
                           f"{self.BASE}/produkt/{p.get('slug', p['sku'])}", cur / 100,
                           std / 100 if std else None,
                           note=f"{pr['discountPercentage']} %" if pr.get("discountPercentage") else None)

    def search(self, query, n=12):
        from urllib.parse import quote
        d = http_get(f"{self.BASE}/api/product-discovery/products/search/{quote(query)}",
                     dict(pageSize=n), {"User-Agent": BROWSER_UA, "Accept": "application/json"})
        return [dict(ref=o["store_ref"], name=o["name"], url=o["url"], offers=[o])
                for o in map(self._offer, d.get("results") or []) if o]

    def fetch(self, refs):
        out, refs = {}, list(refs)
        for i in range(0, len(refs), 30):
            if i: time.sleep(1.5)
            params = [("skus", r) for r in refs[i:i + 30]] + [("pageSize", 30)]
            d = http_get(f"{self.BASE}/api/product-discovery/products", params,
                         {"User-Agent": BROWSER_UA, "Accept": "application/json"})
            for p in d.get("results") or []:
                o = self._offer(p)
                if o: out[o["store_ref"]] = [o]
        return out

    def ref_of(self, store_ref): return store_ref

def luigi(tracker, query, size=12, filters=()):
    """Luigi's Box search (Rossmann + Teta use it). Unknown EANs return fuzzy junk: verify."""
    params = [("tracker_id", tracker), ("q", query), ("size", size)] + [("f[]", f) for f in filters]
    return (http_get("https://live.luigisbox.com/search", params, {"User-Agent": BROWSER_UA})
            .get("results") or {}).get("hits") or []

class Rossmann:
    """VERIFIED 2026-09-23 via Luigi's Box (tracker 715445-966389), no login. Search by name,
    EAN or order number (id_erp); price by id = search q=<id_erp>, one call per item.
    Prices: regular (shelf), clubMember (Rossmann Club app), rossmanekMember (kids club)."""
    aggregator = False
    ean_search = True
    TRACKER = "715445-966389"

    def _offers(self, h):
        a = h.get("attributes") or {}
        reg = a.get("price_regular_CZK_incVat")
        ids = a.get("id_erp") or []
        if reg is None or not ids: return []
        eans = [e.lstrip("0") for e in a.get("id_ean") or []]
        o = store_offer("rossmann", "Rossmann", ids[0],
                        f"{(a.get('PRODUCER') or [''])[0]} {a.get('title', '')}".strip(),
                        pack_size(a.get("title", "")), f"https://www.rossmann.cz/vyhledavani?q={ids[0]}",
                        reg, ean=eans[0] if eans else None)
        o["eans"] = eans
        out = [o]
        for key, label in (("price_clubMember_CZK_incVat", "Rossmann Club"),
                           ("price_rossmanekMember_CZK_incVat", "Rossmánek (kids club)")):
            v = a.get(key)
            if v is not None and v < reg - 0.001:
                out.append(extra_price(o, v, key.split("_")[1], club=label, promo=True))
        return out

    def search(self, query, n=12):
        hits = luigi(self.TRACKER, query, n)
        cands = [offs for offs in map(self._offers, hits) if offs]
        if is_ean(query): cands = [c for c in cands if query.strip().lstrip("0") in c[0]["eans"]]
        return [dict(ref=c[0]["store_ref"], name=c[0]["name"], url=c[0]["url"], offers=c) for c in cands]

    def fetch(self, refs):
        out = {}
        for i, ref in enumerate(refs):
            if i: time.sleep(1.0)
            for offs in map(self._offers, luigi(self.TRACKER, ref, 3)):
                if offs and offs[0]["store_ref"] == ref: out[ref] = offs; break
        return out

    def ref_of(self, store_ref): return store_ref

class Teta:
    """VERIFIED 2026-09-23. Search via Luigi's Box (tracker 407635-780988, name or EAN,
    verify the EAN); price by variant code from be.tetadrogerie.cz (Sylius), no login.
    bbyPrices in haléře: zcmd = shelf, acmd = promo (often conditional, see 'conditions'),
    club = Teta club price."""
    aggregator = False
    ean_search = True
    TRACKER = "407635-780988"
    BE = "https://be.tetadrogerie.cz/api/v2/shop/product-variants/"

    def _offers(self, code, name, prices, ean=None, amount=""):
        if isinstance(prices, str): prices = json.loads(prices)
        zc = (prices or {}).get("zcmd")
        if zc is None: return []
        o = store_offer("teta", "Teta", code, name, amount,
                        f"https://www.tetadrogerie.cz/vyhledavani?q={code}", zc / 100, ean=ean)
        out = [o]
        ac, cl = prices.get("acmd"), prices.get("club")
        if ac is not None and ac < zc:
            out.append(extra_price(o, ac / 100, "akce", note=prices.get("conditions") or None, promo=True))
        if cl is not None and cl < zc:
            out.append(extra_price(o, cl / 100, "club", club="Teta club", promo=True))
        return out

    def search(self, query, n=12):
        cands = []
        for h in luigi(self.TRACKER, query, n, ["type:product_variant"]):
            a = h.get("attributes") or {}
            code, eans = (a.get("code") or [None])[0], a.get("ean") or []
            if not code or (is_ean(query) and query.strip() not in eans): continue
            offs = self._offers(code, a.get("title", ""), a.get("prices") or "{}",
                                eans[0] if eans else None, pack_size(a.get("title", "")))
            if offs: cands.append(dict(ref=code, name=offs[0]["name"], url=offs[0]["url"], offers=offs))
        return cands

    def fetch(self, refs):
        out = {}
        for i, code in enumerate(refs):
            if i: time.sleep(1.0)
            try:
                d = http_get(self.BE + str(code).zfill(18), None,
                             {"User-Agent": BROWSER_UA, "Accept": "application/ld+json"})
            except Exception as e:
                print(f"  [teta] {code}: {e}"); continue
            offs = self._offers(code, d.get("name", ""), d.get("bbyPrices") or {},
                                amount=pack_size(d.get("name", "")))
            if offs: out[code] = offs
        return out

    def ref_of(self, store_ref): return store_ref

# Heureka: not wired yet. Each needs the live-inspect treatment Rohlík and Kupi got
# before it gets a search()/fetch() adapter. Makro: needs MAKRO_COOKIE, shows bez DPH ->
# add product.vat_pct back.

SOURCES = {"globus": Globus, "kosik": Kosik, "tesco": Tesco, "billa": Billa, "dm": Dm,
           "rossmann": Rossmann, "teta": Teta, "makro": Makro, "rohlik": Rohlik, "kupi": Kupi}

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
    for i, o in enumerate(offers):
        if o.get("price_net") is not None and product is not None and product["vat_pct"]:
            o = offers[i] = dict(o, price_incl=round(o["price_net"] * (1 + product["vat_pct"] / 100), 2))
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
    """Upsert Kupi flyer offers into the deal table (the flyer layer). Batched: a crawl
    writes ~11k rows, which must not be one round trip each on Bunny."""
    seen = seen or today()
    stores, rows = {}, []
    for o in offers:
        unit, qty = infer_unit(f"{o.get('product', '')} {o['amount']}", None)
        stores[o["store_id"]] = o["store_name"]
        rows.append((o["ref"], o["slug"], o.get("product"), o["store_id"], o["store_name"],
                     o.get("key"), o["amount"], unit, qty if unit != "pack" else None,
                     o["price_incl"], o["valid_from"], o["promo_until"], o["club"], o["note"],
                     seen, seen))
    c.executemany("INSERT OR IGNORE INTO store(id,name) VALUES (?,?)", list(stores.items()))
    c.executemany("""INSERT INTO deal(ref,slug,product_name,store_id,store_name,pack_key,amount,unit,
                                      unit_qty,price_incl,valid_from,valid_to,club,note,
                                      first_seen,last_seen)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(ref) DO UPDATE SET price_incl=excluded.price_incl,
                       valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                       club=excluded.club, note=excluded.note, amount=excluded.amount,
                       unit=excluded.unit, unit_qty=excluded.unit_qty,
                       last_seen=excluded.last_seen""", rows)

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
        rows = [r for r in c.execute(f"SELECT id, store_id, store_ref FROM offering WHERE source=? AND {col}=?",
                                     (src, val)) if s.ref_of(r["store_ref"]) == ref]
        ids = [r["id"] for r in rows]
        c.executemany("DELETE FROM offering WHERE id=?", [(i,) for i in ids])
        if product:  # no live price left for a store -> its hand-entered price counts again
            for store_id in {r["store_id"] for r in rows}:
                if not c.execute("""SELECT 1 FROM offering WHERE product_id=? AND store_id=?
                                    AND source!='manual' AND active=1""", (product["id"], store_id)).fetchone():
                    c.execute("""UPDATE offering SET active=1 WHERE source='manual' AND store_id=?
                                 AND product_id=?""", (store_id, product["id"]))
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
    cands = search(src, query or (search_text(target) if ean else default_query(target["name"])))
    if not cands: return
    if pick is None:
        raw = input(f"\nWhich is '{target['name']}'?  number, or blank to skip: ").strip()
        if not raw: return
        pick = int(raw)
    cd = cands[pick - 1]
    offers = link(src, cd["ref"], ean, need, qty, cd["name"])
    print(f"matched {target['name']} -> {src}:{cd['ref']} ({cd['name']}), "
          f"{len(offers)} offer(s): " + "; ".join(fmt_offer(o) for o in offers[:4]))

def search_text(p):
    """What to search the stores with for an item: its search_query, else its display name."""
    q = p["search_query"] if "search_query" in p.keys() else None
    return default_query(q or p["name"])

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

# ---------------------------------------------------------------- barcodes by name
# Items without an EAN (e.g. imported from Rohlík, which publishes none) get barcode
# candidates from stores whose name search returns EANs (Globus, Tesco). The user picks one;
# setting the EAN then lets the barcode match (/api/suggest) link every store at once.
EAN_SOURCES = ("globus", "tesco")
EAN_STOP = {"balení", "baleni", "ks", "kus", "bio", "cca", "volně", "vanička", "sáček", "rohlik", "cz",
            "the", "and", "s", "v", "z", "na", "pro", "a", "se", "do"}
LOOSE = re.compile(r"\b1\s*ks\b|volně|volná|volné|volný|svazek|květináč|\bcca\b", re.I)

def _tokens(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return {t for t in re.findall(r"[a-z0-9]+", s) if len(t) > 1 and not t.isdigit()} - EAN_STOP

def ean_score(product, cand_name, cand_amount):
    """0..~1.35: share of our name's words found in the candidate, +0.25 when the pack size
    agrees (-0.25 when it clearly doesn't), -0.4 when the brand (first word) is missing."""
    want = _tokens(search_text(product))
    if not want: return 0.0
    got = _tokens(cand_name)
    score = len(want & got) / len(want)
    brand = _tokens(re.sub(r"^(rohlik\.cz|bio)\s+", "", search_text(product), flags=re.I).split()[0])
    if brand and not brand & got: score -= 0.4
    pct = lambda n: set(re.findall(r"(\d+(?:[.,]\d+)?)\s*%", n.replace(",", ".")))
    ours = product["search_query"] or product["name"]
    if pct(ours) and pct(cand_name) and not pct(ours) & pct(cand_name):
        score -= 0.4                                  # 0 % vs 5 % yogurt: a different product
    own = re.match(r"(tesco|globus|váš výběr|vas vyber|k-classic|clever|billa)\b", cand_name.strip(), re.I)
    if own and not re.match(re.escape(own[1]), (product["search_query"] or product["name"]).strip(), re.I):
        score -= 0.4                                  # a chain's own brand only fits that chain
    unit, qty = infer_unit(f"{cand_name} {cand_amount}", None)
    same = unit == product["unit"] or {unit, product["unit"]} == {"kg", "l"}  # 450 g kefír = 450 ml
    if same and product["unit_qty"]:
        ratio = qty / product["unit_qty"]
        score += 0.25 if 0.95 <= ratio <= 1.05 else (-0.25 if ratio < 0.8 or ratio > 1.25 else 0)
    return round(score, 3)

def ean_candidates(product, per_source=3, min_score=0.6):
    """Search the EAN-returning stores by name; best-scoring hits that carry an EAN."""
    out = []
    q = search_text(product)
    queries = [q] + ([" ".join(q.split()[:3])] if len(q.split()) > 3 else [])
    for name in EAN_SOURCES:
        src, hits = source(name), []
        for query in queries:  # full name first, then a shorter one if nothing came back
            try:
                hits = [o for cd in src.search(query) for o in cd["offers"][:1] if o.get("ean")]
            except Exception as e:
                print(f"  [{name}] {e}"); hits = []
            time.sleep(3 if name == "globus" else 1.5)  # globus robots.txt: crawl-delay 3
            if hits: break
        scored = sorted(((ean_score(product, o["name"], o["amount"]), o) for o in hits),
                        key=lambda x: -x[0])
        out += [dict(source=name, ean=o["ean"], name=o["name"], amount=o["amount"],
                     price=o["price_incl"], score=sc) for sc, o in scored[:per_source] if sc >= min_score]
    merged = {}
    for x in sorted(out, key=lambda x: -x["score"]):  # same EAN from both stores = one, stronger
        if x["ean"] in merged:
            m = merged[x["ean"]]; m["sources"].append(x["source"]); m["score"] = round(m["score"] + 0.1, 3)
        else:
            merged[x["ean"]] = dict(x, sources=[x["source"]])
    return sorted(merged.values(), key=lambda x: -x["score"])

def find_eans(redo=False):
    """Background job: barcode candidates for every item without an EAN (loose produce skipped).
    ~3.5 s an item (Globus asks for 3 s between requests). Already-searched items are skipped
    unless redo."""
    c = conn()
    done = set() if redo else {r["product_id"] for r in c.execute("SELECT product_id FROM ean_lookup")}
    todo = [p for p in c.execute("SELECT * FROM product WHERE ean IS NULL ORDER BY id")
            if p["id"] not in done and not LOOSE.search(p["name"])]
    print(f"looking up barcodes for {len(todo)} items (~{len(todo) * 3.5 / 60:.0f} min)")
    found = 0
    for i, p in enumerate(todo, 1):
        cands = ean_candidates(p)
        c.execute("DELETE FROM ean_candidate WHERE product_id=?", (p["id"],))
        c.executemany("""INSERT INTO ean_candidate(product_id,ean,sources,name,amount,price,score)
                         VALUES (?,?,?,?,?,?,?)""",
                      [(p["id"], x["ean"], ",".join(x["sources"]), x["name"], x["amount"], x["price"],
                        x["score"]) for x in cands[:5]])
        c.execute("INSERT OR REPLACE INTO ean_lookup(product_id,ts,found,dismissed) VALUES (?,?,?,0)",
                  (p["id"], today(), len(cands)))
        found += bool(cands)
        c.commit()
        print(f"  {i}/{len(todo)}  {p['name'][:40]:40} {len(cands)} candidate(s)")
    c.close()
    print(f"barcodes done: candidates for {found} of {len(todo)} items")

def ean_dismiss(product_id):
    c = conn()
    c.execute("DELETE FROM ean_candidate WHERE product_id=?", (product_id,))
    c.execute("UPDATE ean_lookup SET dismissed=1 WHERE product_id=?", (product_id,))
    c.commit(); c.close()

# ---------------------------------------------------------------- products (add / edit / delete)
PRODUCT_FIELDS = ("ean", "name", "category", "vat_pct", "unit", "unit_qty", "pack_note", "search_query")

def product_add(name, category=None, vat_pct=None, ean=None, unit=None, unit_qty=None, pack_note=None,
                search_query=None):
    """New item to track. Unit + units-per-pack are inferred from the name (or the search text,
    which usually carries the pack size) unless given."""
    name = (name or "").strip()
    if not name: raise ValueError("name is required")
    search_query = (search_query or "").strip() or None
    u, q = infer_unit(name, category)
    if u == "pack" and search_query: u, q = infer_unit(search_query, category)
    c = conn()
    try:
        cur = c.execute("""INSERT INTO product(ean,name,category,vat_pct,unit,unit_qty,pack_note,search_query)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        ((ean or "").strip() or None, name, category or None,
                         int(vat_pct) if vat_pct not in (None, "") else None,
                         unit or u, float(unit_qty) if unit_qty not in (None, "") else q, pack_note,
                         search_query))
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"an item with EAN {ean} already exists")
    finally:
        c.close()

def product_update(pid, **fields):
    """Edit an item. Empty clears ean / search_query / category / pack_note; empty name, unit,
    unit_qty or VAT are ignored. Renaming an item that has no search text keeps the old name as
    its search text, so e.g. an English display name still searches the stores in Czech."""
    sets = {k: v for k, v in fields.items() if k in PRODUCT_FIELDS}
    for k in ("name", "unit", "unit_qty", "vat_pct"):
        if k in sets and str(sets[k]).strip() == "": del sets[k]
    for k in ("ean", "search_query", "category", "pack_note"):
        if k in sets: sets[k] = (sets[k] or "").strip() or None
    if "unit_qty" in sets: sets["unit_qty"] = float(sets["unit_qty"])
    if "vat_pct" in sets: sets["vat_pct"] = int(sets["vat_pct"])
    if not sets: return
    c = conn()
    old = c.execute("SELECT name, search_query FROM product WHERE id=?", (pid,)).fetchone()
    if old and "name" in sets and sets["name"] != old["name"] and not sets.get("search_query") \
            and not old["search_query"]:
        sets["search_query"] = old["name"]
    try:
        c.execute(f"UPDATE product SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                  (*sets.values(), pid))
        if sets.get("ean"): c.execute("DELETE FROM ean_candidate WHERE product_id=?", (pid,))
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

def suggest(product_id):
    """Barcode matches for an item: ask every EAN-capable store for its EAN and return the
    hits not linked yet, for one-click confirmation. Košík doesn't echo the EAN back, and a
    single-item EAN can land on a multipack there, so its pack size is shown for checking."""
    c = conn()
    p = c.execute("SELECT * FROM product WHERE id=?", (product_id,)).fetchone()
    linked = {(w["source"], w["ref"]) for w in c.execute(
        "SELECT source, ref FROM watch WHERE product_id=?", (product_id,))}
    c.close()
    if not p or not p["ean"]: return []
    out = []
    for name, cls in SOURCES.items():
        by_name = getattr(cls, "ean_via_name", False)  # no EAN search, but results carry the EAN
        if not (getattr(cls, "ean_search", False) or by_name): continue
        try:
            cands = cls().search(search_text(p) if by_name else p["ean"])
        except Exception as e:
            print(f"  [suggest {name}] {e}"); continue
        for cd in cands:
            eans = {o.get("ean") for o in cd["offers"]} | ({cd["ref"]} if name == "makro" else set())
            eans |= {e for o in cd["offers"] for e in o.get("eans") or []}
            if name != "kosik" and p["ean"] not in eans: continue  # exact barcode only
            if (name, cd["ref"]) in linked: continue
            out.append(dict(cd, source=name))
            if name == "kosik": break  # first hit only; its EAN isn't in the response
        time.sleep(0.5)
    return out

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

# ---------------------------------------------------------------- shopping list
def matrix_cell(rows, stores, row_key, store_id, kind):
    """(row, snapshot dict) for a matrix cell, or (row, None) if it has no price now."""
    row = next((r for r in rows if r["key"] == row_key), None)
    if not row: return None, None
    names = {s["id"]: s["name"] for s in stores}
    if kind == "flyer":
        d = row.get("deal")
        if not d or d["store_id"] != store_id: return row, None
        return row, dict(store_name=d["store_name"] or store_id, item_name=d["product_name"],
                         amount=d["amount"], price_incl=d["price_incl"], unit_price=d["unit_price"],
                         promo_until=d["valid_to"], club=d["club"], note=d["note"], url=d["url"])
    cell = row["cells"].get(store_id)
    if not cell: return row, None
    return row, dict(store_name=names.get(store_id, store_id), item_name=cell["offering_name"],
                     amount=None, price_incl=cell["price_incl"], unit_price=cell["unit_price"],
                     promo_until=cell["promo_until"], club=cell["club"], note=cell["note"],
                     url=cell["url"])

def cart_toggle(row_key, store_id, kind="store"):
    """Tap a matrix cell: add that item at that store; tap again to remove. A row is in the
    list at one store at a time, so tapping another store's cell moves it there."""
    stores, rows = matrix_rows()
    row, snap = matrix_cell(rows, stores, row_key, store_id, kind)
    if not row or not snap: raise ValueError("that cell has no current price")
    c = conn()
    cur = c.execute("SELECT store_id, kind FROM cart_item WHERE row_key=?", (row_key,)).fetchone()
    if cur and cur["store_id"] == store_id and cur["kind"] == kind:
        c.execute("DELETE FROM cart_item WHERE row_key=?", (row_key,)); c.commit(); c.close()
        return False
    c.execute("""INSERT INTO cart_item(row_key,row_name,unit,store_id,store_name,kind,item_name,amount,
                                       price_incl,unit_price,promo_until,club,note,url,qty,checked,added)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?)
                 ON CONFLICT(row_key) DO UPDATE SET store_id=excluded.store_id,
                   store_name=excluded.store_name, kind=excluded.kind, item_name=excluded.item_name,
                   amount=excluded.amount, price_incl=excluded.price_incl,
                   unit_price=excluded.unit_price, promo_until=excluded.promo_until,
                   club=excluded.club, note=excluded.note, url=excluded.url, checked=0""",
              (row_key, row["name"], row["unit"], store_id, snap["store_name"], kind, snap["item_name"],
               snap["amount"], snap["price_incl"], snap["unit_price"], snap["promo_until"],
               snap["club"], snap["note"], snap["url"], today()))
    c.commit(); c.close()
    return True

def cart_items():
    """The list, with each line's current price at the same store (None if it's gone)."""
    stores, rows = matrix_rows()
    c = conn()
    items = [dict(r) for r in c.execute("SELECT * FROM cart_item ORDER BY store_name, row_name")]
    c.close()
    for it in items:
        _, snap = matrix_cell(rows, stores, it["row_key"], it["store_id"], it["kind"])
        it["current_price"] = snap["price_incl"] if snap else None
    return items

def cart_update(item_id, qty=None, checked=None):
    c = conn()
    if qty is not None:
        c.execute("UPDATE cart_item SET qty=? WHERE id=?", (max(1, int(qty)), item_id))
    if checked is not None:
        c.execute("UPDATE cart_item SET checked=? WHERE id=?", (int(bool(checked)), item_id))
    c.commit(); c.close()

def cart_remove(item_id=None, checked_only=False, everything=False):
    c = conn()
    if item_id is not None: c.execute("DELETE FROM cart_item WHERE id=?", (item_id,))
    elif checked_only: c.execute("DELETE FROM cart_item WHERE checked=1")
    elif everything: c.execute("DELETE FROM cart_item")
    c.commit(); c.close()

def cart_save(name=None):
    """Save the current list (as it is, prices included) and start a fresh one."""
    c = conn()
    items = [dict(r) for r in c.execute("SELECT * FROM cart_item ORDER BY store_name, row_name")]
    if not items: c.close(); raise ValueError("the list is empty")
    total = round(sum((i["price_incl"] or 0) * i["qty"] for i in items), 2)
    name = (name or "").strip() or f"Nákup {dt.date.today():%-d. %-m. %Y}"
    cur = c.execute("INSERT INTO saved_list(name,created,total,items) VALUES (?,?,?,?)",
                    (name, dt.datetime.now().isoformat(timespec="minutes"), total,
                     json.dumps(items, ensure_ascii=False)))
    c.execute("DELETE FROM cart_item")
    c.commit(); c.close()
    return cur.lastrowid

def saved_lists():
    c = conn()
    out = [dict(id=r["id"], name=r["name"], created=r["created"], total=r["total"],
                count=len(json.loads(r["items"])))
           for r in c.execute("SELECT * FROM saved_list ORDER BY created DESC")]
    c.close()
    return out

def saved_list(list_id):
    c = conn()
    r = c.execute("SELECT * FROM saved_list WHERE id=?", (list_id,)).fetchone()
    c.close()
    if not r: raise ValueError("no such list")
    return dict(id=r["id"], name=r["name"], created=r["created"], total=r["total"], items=json.loads(r["items"]))

def saved_list_reuse(list_id):
    """Put a saved list back on the current list: same items at the same stores, today's
    prices where the store still has one (else the saved snapshot). Existing lines win."""
    stores, rows = matrix_rows()
    lst = saved_list(list_id)
    c = conn()
    have = {r["row_key"] for r in c.execute("SELECT row_key FROM cart_item")}
    added = 0
    for it in lst["items"]:
        if it["row_key"] in have: continue
        _, snap = matrix_cell(rows, stores, it["row_key"], it["store_id"], it["kind"])
        it = dict(it, **snap) if snap else it
        c.execute("""INSERT INTO cart_item(row_key,row_name,unit,store_id,store_name,kind,item_name,amount,
                                           price_incl,unit_price,promo_until,club,note,url,qty,checked,added)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                  (it["row_key"], it["row_name"], it["unit"], it["store_id"], it["store_name"], it["kind"],
                   it["item_name"], it["amount"], it["price_incl"], it["unit_price"], it["promo_until"],
                   it["club"], it["note"], it["url"], it["qty"], today()))
        added += 1
    c.commit(); c.close()
    return added

def saved_list_delete(list_id):
    c = conn(); c.execute("DELETE FROM saved_list WHERE id=?", (list_id,)); c.commit(); c.close()

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
    ecand = {}
    for r in c.execute("SELECT * FROM ean_candidate ORDER BY score DESC"):
        ecand.setdefault(r["product_id"], []).append(dict(r))
    deals = [dict(r) for r in c.execute("""SELECT product_id, slug, store_id, store_name, amount,
                                                  price_incl, valid_from, valid_to AS promo_until,
                                                  club, note FROM v_deal
                                           WHERE product_id IS NOT NULL ORDER BY price_incl""")]
    c.close()
    for p in prods:
        p["query"] = search_text(p)
        p["ean_candidates"] = ecand.get(p["id"], []) if not p["ean"] else []
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
    import threading, io
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    JOBS = {"crawl": crawl, "eans": find_eans}  # long runs, one thread each, log kept for the UI
    jobs = {name: {"thread": None, "log": io.StringIO()} for name in JOBS}

    class JobLog(io.StringIO):  # print() from the job thread lands here (stdout is swapped per thread)
        pass
    _stdout = sys.stdout
    class ThreadStdout:
        def write(self, s): return getattr(threading.current_thread(), "joblog", _stdout).write(s)
        def flush(self): pass
    sys.stdout = ThreadStdout()

    def start_job(name):
        j = jobs[name]
        if j["thread"] and j["thread"].is_alive(): return False
        j["log"] = log = JobLog()
        def work():
            threading.current_thread().joblog = log
            try: JOBS[name]()
            except Exception as e: print(f"{name} failed: {e}")
        j["thread"] = threading.Thread(target=work, daemon=True)
        j["thread"].start()
        return True

    def job_status(name):
        t = jobs[name]["thread"]
        return dict(running=bool(t and t.is_alive()), log=jobs[name]["log"].getvalue()[-2000:])

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

        def end_headers(self):
            if not self.path.startswith("/api/"):  # pages change with updates: always revalidate
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

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
                    c = conn()
                    cart = {r["row_key"]: dict(store_id=r["store_id"], kind=r["kind"])
                            for r in c.execute("SELECT row_key, store_id, kind FROM cart_item")}
                    return self._json(dict(generated=dt.datetime.now().isoformat(timespec="minutes"),
                                           stores=stores, rows=rows, cart=cart))
                if u.path == "/api/cart":
                    return self._json(dict(items=cart_items()))
                if u.path == "/api/lists":
                    return self._json(dict(lists=saved_lists()))
                if u.path == "/api/list":
                    return self._json(saved_list(int(q["id"])))
                if u.path == "/api/products":
                    return self._json(dict(products=products_status(), sources=list(SOURCES)))
                if u.path in ("/api/crawl", "/api/eans"):
                    return self._json(job_status(u.path.rsplit("/", 1)[1]))
                if u.path == "/api/suggest":
                    return self._json(dict(results=suggest(int(q["product_id"]))))
                if u.path == "/api/lookup":
                    return self._json(dict(results=lookup(q["q"])))
                if u.path == "/api/search":
                    n = max(1, min(int(q.get("n", 60)), 150))
                    return self._json(dict(candidates=source(q["source"]).search(q["q"], n), n=n))
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
                if u == "/api/cart/toggle":
                    return self._json(dict(ok=True, in_cart=cart_toggle(a["row_key"], a["store_id"],
                                                                         a.get("kind", "store"))))
                if u == "/api/cart/save":
                    return self._json(dict(ok=True, id=cart_save(a.get("name"))))
                if u == "/api/lists/reuse":
                    return self._json(dict(ok=True, added=saved_list_reuse(a["id"])))
                if u == "/api/lists/delete":
                    saved_list_delete(a["id"])
                    return self._json(dict(ok=True))
                if u == "/api/cart/update":
                    cart_update(a["id"], a.get("qty"), a.get("checked"))
                    return self._json(dict(ok=True))
                if u == "/api/cart/remove":
                    cart_remove(a.get("id"), a.get("checked_only", False), a.get("all", False))
                    return self._json(dict(ok=True))
                if u in ("/api/crawl", "/api/eans"):
                    return self._json(dict(ok=True, started=start_job(u.rsplit("/", 1)[1])))
                if u == "/api/eans/dismiss":
                    ean_dismiss(a["product_id"])
                    return self._json(dict(ok=True))
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
    for name in ("init", "seed", "matrix", "export", "migrate-to-bunny"): sub.add_parser(name)
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
    fe = sub.add_parser("find-eans", help="barcode candidates by name for items without one")
    fe.add_argument("--redo", action="store_true", help="search already-searched items again")
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
    elif a.cmd == "find-eans": find_eans(a.redo)
    elif a.cmd == "fetch": fetch(a.source.split(",") if a.source else None)
    elif a.cmd == "matrix": matrix()
    elif a.cmd == "export": export()
    elif a.cmd == "migrate-to-bunny": migrate_to_bunny()
    elif a.cmd == "serve": serve(a.port)
    elif a.cmd == "need" and a.ncmd == "add": need_add(a.name, a.unit, a.category)
    elif a.cmd == "need" and a.ncmd == "link": need_link(a.name, a.eans, a.offering)
    elif a.cmd == "store" and a.scmd == "member": store_member(a.store, a.state == "on")

if __name__ == "__main__":
    main()
