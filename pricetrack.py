#!/usr/bin/env python3
"""
pricetrack - a personal grocery price tracker.

Matches a watchlist by EAN, pulls current + promo prices across Czech sources,
keeps a price history, and reports the CHEAPEST source per item vs the item's
30-day average across ALL stores. No store is treated as a baseline.

Backends: local SQLite (default) or Bunny Database (set PRICETRACK_BACKEND=bunny
and BUNNY_DATABASE_URL / BUNNY_DATABASE_AUTH_TOKEN). See bunny_seed.mjs for the
TS/Edge path.

Usage:
    python pricetrack.py init | seed | fetch [--source ...] | export | report
"""
import argparse, csv, json, os, sqlite3, time, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "prices.db"
WATCHLIST = ROOT / "watchlist.csv"
EXPORT = ROOT / "dashboard" / "prices.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS item (
    ean TEXT PRIMARY KEY, name TEXT, category TEXT, vat_pct INTEGER,
    makro_price_incl REAL, pack_note TEXT
);
CREATE TABLE IF NOT EXISTS quote (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ean TEXT, source TEXT, price_incl REAL, is_promo INTEGER DEFAULT 0,
    url TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_quote_ean_ts ON quote(ean, ts);
"""

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def count(table):
    c = conn(); n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]; c.close(); return n

def init():
    c = conn(); c.executescript(SCHEMA)
    with open(WATCHLIST, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            c.execute("INSERT OR REPLACE INTO item VALUES (?,?,?,?,?,?)",
                (r["ean"], r["name"], r["category"], int(r["vat_pct"]),
                 float(r["makro_price_incl"]) if r["makro_price_incl"] else None, r["pack_note"]))
    c.commit(); c.close()
    print(f"init ok -> {DB} ({count('item')} items)")

def record(ean, source, price_incl, is_promo=False, url="", ts=None):
    ts = ts or dt.date.today().isoformat()
    c = conn()
    c.execute("DELETE FROM quote WHERE ean=? AND source=? AND ts=?", (ean, source, ts))
    c.execute("INSERT INTO quote(ean,source,price_incl,is_promo,url,ts) VALUES (?,?,?,?,?,?)",
              (ean, source, round(price_incl, 2), 1 if is_promo else 0, url, ts))
    c.commit(); c.close()

# ---------------------------------------------------------------- sources
def src_rohlik(ean, name):
    """VERIFIED against the live site. Rohlik has no EAN search, so we match by
    name via autocomplete, then read the product card for price + promo.
    Prices include VAT. Set ROHLIK_COOKIE to pin prices to your delivery region."""
    import requests, re
    BASE = "https://www.rohlik.cz"
    h = {"User-Agent": "personal-pricetrack/1.0", "Accept": "application/json"}
    if os.environ.get("ROHLIK_COOKIE"):
        h["Cookie"] = os.environ["ROHLIK_COOKIE"]
    out = []
    try:
        ac = requests.get(f"{BASE}/services/frontend-service/autocomplete",
                          params={"search": name, "referer": "whisperer", "companyId": 1},
                          headers=h, timeout=15)
        ac.raise_for_status()
        ids = ac.json().get("productIds", [])[:8]
        if not ids:
            return out
        params = [("products", i) for i in ids] + [("categoryType", "normal")]
        cr = requests.get(f"{BASE}/api/v1/products/card", params=params, headers=h, timeout=15)
        cr.raise_for_status()
        cards = cr.json()
        # pick the best name match (token overlap) among candidates
        want = set(re.findall(r"\w+", name.lower()))
        def score(c): return len(want & set(re.findall(r"\w+", c.get("name","").lower())))
        best = max(cards, key=score, default=None)
        if best:
            p = best.get("prices", {})
            sale, orig = p.get("salePrice"), p.get("originalPrice")
            cur = sale if sale is not None else orig
            if cur is not None:
                out.append(dict(price_incl=float(cur),
                                is_promo=bool(p.get("saleId")) or (sale is not None and orig and sale < orig),
                                url=f"{BASE}/{best.get('productId','')}"))
    except Exception as e:
        print(f"  [rohlik] {ean}: {e}")
    return out

def src_kupi(ean, name):
    import requests
    from bs4 import BeautifulSoup
    out = []
    try:
        r = requests.get("https://www.kupi.cz/hledej", params={"f": name}, timeout=15,
                         headers={"User-Agent": "personal-pricetrack/1.0"})
        r.raise_for_status()
        node = BeautifulSoup(r.text, "html.parser").select_one(".product_offer .price, .goods_price")
        if node:
            txt = node.get_text(strip=True).replace("Kč", "").replace(",", ".")
            out.append(dict(price_incl=float("".join(ch for ch in txt if ch.isdigit() or ch=="." )),
                            is_promo=True, url="https://www.kupi.cz/hledej?f=" + name.replace(" ", "+")))
    except Exception as e:
        print(f"  [kupi] {ean}: {e}")
    return out

def src_heureka(ean, name):
    import requests
    from bs4 import BeautifulSoup
    out = []
    try:
        r = requests.get("https://www.heureka.cz/", params={"h[fraze]": ean}, timeout=15,
                         headers={"User-Agent": "personal-pricetrack/1.0"})
        r.raise_for_status()
        node = BeautifulSoup(r.text, "html.parser").select_one(".c-min-price, .price")
        if node:
            txt = node.get_text(strip=True).replace("Kč","").replace("\xa0","").replace(",",".")
            out.append(dict(price_incl=float("".join(ch for ch in txt if ch.isdigit() or ch=="." )),
                            is_promo=False, url="https://www.heureka.cz/?h[fraze]=" + ean))
    except Exception as e:
        print(f"  [heureka] {ean}: {e}")
    return out

def src_makro(ean, name):
    """Makro is just one more store now. Needs your session cookie (MAKRO_COOKIE).
    Makro shows price bez DPH; add the item's VAT back before recording."""
    import requests
    out = []
    cookie = os.environ.get("MAKRO_COOKIE")
    if not cookie:
        return out
    try:
        requests.get("https://sortiment.makro.cz/cs/", params={"query": ean}, timeout=15,
                     headers={"User-Agent": "personal-pricetrack/1.0", "Cookie": cookie})
        # parse tile price (verify selector), convert bez->incl using item vat, then append.
    except Exception as e:
        print(f"  [makro] {ean}: {e}")
    return out

SOURCES = {"rohlik": src_rohlik, "kupi": src_kupi, "heureka": src_heureka, "makro": src_makro}

def fetch(which):
    which = which or list(SOURCES)
    c = conn(); items = c.execute("SELECT * FROM item").fetchall(); c.close()
    for it in items:
        for name in which:
            for q in SOURCES[name](it["ean"], it["name"]):
                record(it["ean"], name, q["price_incl"], q["is_promo"], q["url"])
                print(f"  {name:8} {it['name'][:34]:34} {q['price_incl']:7.2f}"
                      f"{' (promo)' if q['is_promo'] else ''}")
            time.sleep(1.2)
    print("fetch done")

# ---------------------------------------------------------------- seed (offline)
def seed():
    """Load hand-verified prices so the dashboard has real data before any fetch.
    Makro is recorded as one store among several, not a baseline."""
    today = dt.date.today().isoformat()
    c = conn()
    for it in c.execute("SELECT * FROM item").fetchall():
        if it["makro_price_incl"]:
            record(it["ean"], "makro", it["makro_price_incl"], False, "https://sortiment.makro.cz", today)
    c.close()
    verified = {
        "8006540858073": [("dm", 299.0, False, "https://www.dm.cz"),
                           ("rossmann_pl", 335.0, False, "https://www.rossmann.pl")],
        "7610400013864": [("heureka", 129.0, False, "https://www.heureka.cz"),
                           ("lindt", 219.9, False, "https://www.lindt.cz")],
        "8593803421216": [("kaufland", 34.9, True, "https://www.kupi.cz")],
        "8594006091329": [("tesco", 34.9, True, "https://www.itesco.cz")],
        "4014400762235": [("penny", 29.9, True, "https://www.kupi.cz")],
        "8718951801400": [("dm", 159.0, False, "https://www.dm.cz")],
        "8720181481529": [("teta", 94.9, True, "https://www.tetadrogerie.cz")],
        "8584014858502": [("alza", 179.0, False, "https://www.alza.cz")],
        "8594001021666": [("tesco", 14.9, True, "https://www.itesco.cz")],
        "8435409374261": [("kaufland", 167.5, False, "https://www.kaufland.cz")],
        "8411135006829": [("heureka", 459.0, False, "https://www.heureka.cz")],
    }
    for ean, quotes in verified.items():
        for source, price, promo, url in quotes:
            record(ean, source, price, promo, url, today)
    print(f"seed ok ({count('quote')} quotes)")

# ---------------------------------------------------------------- analytics
def stats_for(ean):
    """Cheapest source today vs the 30-day average across ALL stores."""
    c = conn()
    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    rows = c.execute("SELECT * FROM quote WHERE ean=? AND ts>=? ORDER BY ts DESC",
                     (ean, cutoff)).fetchall()
    c.close()
    if not rows:
        return None
    latest = rows[0]["ts"]
    todays = [r for r in rows if r["ts"] == latest]
    best = min(todays, key=lambda r: r["price_incl"])
    prices = [r["price_incl"] for r in rows]
    avg30 = round(sum(prices) / len(prices), 2)
    return dict(best_source=best["source"], best_price=best["price_incl"],
                best_is_promo=bool(best["is_promo"]), best_url=best["url"],
                avg30=avg30, saving_vs_avg=round(avg30 - best["price_incl"], 2),
                n_quotes_30d=len(rows),
                all_today=sorted([dict(source=r["source"], price=r["price_incl"],
                                       promo=bool(r["is_promo"]), url=r["url"]) for r in todays],
                                 key=lambda q: q["price"]))

def export():
    c = conn(); items = c.execute("SELECT * FROM item ORDER BY category,name").fetchall(); c.close()
    payload, total = [], 0.0
    for it in items:
        s = stats_for(it["ean"])
        if not s: continue
        total += max(s["saving_vs_avg"], 0)
        payload.append(dict(ean=it["ean"], name=it["name"], category=it["category"],
                            pack_note=it["pack_note"], **s))
    EXPORT.parent.mkdir(exist_ok=True)
    EXPORT.write_text(json.dumps(dict(
        generated=dt.datetime.now().isoformat(timespec="minutes"),
        basket_saving_vs_avg=round(total, 2), items=payload),
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"export ok -> {EXPORT}  (basket saving vs 30-day avg: {total:.0f} Kc)")

def report():
    c = conn(); items = c.execute("SELECT * FROM item ORDER BY category,name").fetchall(); c.close()
    print("\n%-38s %7s %-13s %7s %7s" % ("ITEM","BEST","WHERE","AVG30","VS AVG"))
    print("-" * 78)
    tot = 0.0
    for it in items:
        s = stats_for(it["ean"])
        if not s: continue
        tot += max(s["saving_vs_avg"], 0)
        promo = "*" if s["best_is_promo"] else " "
        print("%-38s %6.0f%s %-13s %7.0f %6.0f" % (
            it["name"][:38], s["best_price"], promo, s["best_source"][:13], s["avg30"], s["saving_vs_avg"]))
    print("-" * 78)
    print("Basket saving vs 30-day average (each at today's cheapest): %.0f Kc" % tot)
    print("(* = promo price, may be time-limited)\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init","seed","fetch","export","report"])
    ap.add_argument("--source", help="comma list: rohlik,kupi,heureka,makro")
    a = ap.parse_args()
    {"init":init,"seed":seed,"export":export,"report":report}.get(a.cmd, lambda: None)()
    if a.cmd == "fetch":
        fetch(a.source.split(",") if a.source else None)

if __name__ == "__main__":
    main()
