# Košík — personal grocery price tracker

Tracks grocery prices across Czech stores (Rohlík, Makro, dm, Kaufland…), shows the
cheapest per item in a store matrix, and builds a shopping-list-by-store. Personal use
for one household. **Not a product — never redistribute scraped prices.**

See **CLAUDE.md** for the full design (three-layer data model, matrix view, basket,
verified Rohlík API, Bunny architecture). Start there.

## Layout
```
watchlist.csv          32 items seeded from a real Makro receipt (EANs, VAT, packs)
pricetrack.py          local SQLite tool: init | seed | fetch | export | report
dashboard/index.html   placeholder store-agnostic view (real MATRIX view is TODO)
bunny/
  schema.sql           SQLite-compatible schema (ports straight to Bunny Database)
  seed.mjs             load schema + watchlist into Bunny (auto-loads .env)
  rohlik_probe.py      standalone, VERIFIED live Rohlík price puller
requirements.txt       requests, beautifulsoup4
.env                   Bunny creds (gitignored — never commit)
```

## Quick start (local SQLite)
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python pricetrack.py init && python pricetrack.py seed
python pricetrack.py report
python pricetrack.py fetch --source rohlik    # live Rohlík pull (verified)
python pricetrack.py export                    # writes dashboard/prices.json
cd dashboard && python3 -m http.server         # then open localhost:8000
```

## Prove live pulling
```bash
python bunny/rohlik_probe.py "pampers premium care 1"
```

## Status
- Rohlík adapter: **verified & working**.
- Kupi / Heureka / Makro adapters: **unverified scaffolds** — wire each against the live
  site (DevTools → Network), same as Rohlík. See CLAUDE.md.
- Dashboard: placeholder; build the real store-matrix + basket next.
- Storage: local SQLite works; Bunny Database `kosik` created + seeded.

## Do NOT commit
`.env`, `.bunny/`, `prices.db`, `node_modules/`. All gitignored. If the DB token ever
leaks, rotate it in the Bunny dashboard.
