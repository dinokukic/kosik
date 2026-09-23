#!/usr/bin/env python3
"""
Standalone Rohlik probe - run on your Mac to prove live pulling works:
    python bunny/rohlik_probe.py "pampers premium care 1"
    python bunny/rohlik_probe.py "lindt lisky orisek 300"

Verified against the live site (logged-in Chrome inspection):
- Rohlik has NO EAN search. You must search by NAME.
  autocomplete: GET /services/frontend-service/autocomplete?search=<q>&referer=whisperer&companyId=1
                -> {"productIds":[...], ...}
- Prices/name/promo come from the product "card":
  GET /api/v1/products/card?products=<id>&products=<id>&categoryType=normal
      -> [{productId, name, textualAmount, badges,
           prices:{originalPrice, salePrice, unitPrice, saleId, saleValidTill, currency}, stock}]
  salePrice is the price you pay (VAT included); promo when salePrice < originalPrice.
"""
import sys, requests

BASE = "https://www.rohlik.cz"
H = {"User-Agent": "personal-pricetrack/1.0", "Accept": "application/json"}

def autocomplete(query, n=8):
    r = requests.get(f"{BASE}/services/frontend-service/autocomplete",
                     params={"search": query, "referer": "whisperer", "companyId": 1},
                     headers=H, timeout=15)
    r.raise_for_status()
    return r.json().get("productIds", [])[:n]

def cards(ids):
    if not ids:
        return []
    params = [("products", i) for i in ids] + [("categoryType", "normal")]
    r = requests.get(f"{BASE}/api/v1/products/card", params=params, headers=H, timeout=15)
    r.raise_for_status()
    return r.json()

def price_of(card):
    p = card.get("prices", {})
    sale, orig = p.get("salePrice"), p.get("originalPrice")
    current = sale if sale is not None else orig
    is_promo = bool(p.get("saleId")) or (sale is not None and orig is not None and sale < orig)
    return current, is_promo, p.get("unitPrice"), p.get("saleValidTill")

if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "pampers premium care 1"
    ids = autocomplete(q)
    print(f"query: {q!r}  ->  {len(ids)} product ids: {ids}\n")
    for c in cards(ids):
        cur, promo, unit, till = price_of(c)
        tag = f"  PROMO (till {till})" if promo else ""
        print(f"  [{c['productId']}] {c['name'][:52]:52} {cur:>8.2f} CZK  "
              f"({unit}/{c.get('unit','?')}){tag}")
