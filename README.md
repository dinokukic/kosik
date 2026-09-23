# Košík — personal grocery price tracker

Tracks grocery prices across Czech stores (Rohlík, Makro, dm, Kaufland…), shows the
cheapest per item in a store matrix, and builds a shopping-list-by-store. Personal use
for one household. **Not a product — never redistribute scraped prices.**

See **CLAUDE.md** for the full design (three-layer data model, matrix view, basket,
verified Rohlík API, Bunny architecture). Start there.

## Layout
```
watchlist.csv          32 products from a real Makro receipt (EANs, VAT, packs, units)
pricetrack.py          local SQLite tool: init | seed | search | match | fetch | need | matrix | export
dashboard/index.html   store matrix: price per unit, cheapest per row marked
dashboard/match.html   match screen: pick each item's product per source (needs `serve`)
dashboard/style.css    shared styles
bunny/
  schema.sql           three-layer schema + views, shared by local SQLite and Bunny
  seed.mjs             load schema + stores + products into Bunny (auto-loads .env)
  rohlik_probe.py      standalone, VERIFIED live Rohlík price puller
requirements.txt       requests
package.json           @bunny.net/database-client (for bunny/seed.mjs)
.env                   Bunny creds (gitignored — never commit)
```

## Quick start (local SQLite)
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python pricetrack.py init && python pricetrack.py seed
python pricetrack.py serve                     # localhost:8765 — matrix + match screen
python pricetrack.py matrix                    # rows x stores in the terminal

# match once per item (you pick the right pack), then fetch by id forever
python pricetrack.py match rohlik 8593803421216 --query "jihočeské máslo 250"
python pricetrack.py match kupi 8593803421216  # flyer offers at every chain (Kupi = aggregator)
python pricetrack.py fetch                     # all sources: rohlik + kupi
python pricetrack.py store member kaufland off # don't count Kaufland Card prices

# needs: group different brands/packs into one comparable row
python pricetrack.py need add "Máslo 82%" --unit kg --category dairy
python pricetrack.py need link "Máslo 82%" 8593803421216
python pricetrack.py match rohlik --need "Máslo 82%" --query "máslo 82 250"

python pricetrack.py export                    # writes dashboard/prices.json
cd dashboard && python3 -m http.server         # then open localhost:8000
```

## Prove live pulling
```bash
python bunny/rohlik_probe.py "pampers premium care 1"
```

## Status
- Rohlík adapter: **verified & working**.
- Kupi (flyer aggregator): **verified & working** — offers filed under the real chain.
- Heureka / Makro: **not wired** — inspect the live site first, same as Rohlík/Kupi.
- Schema: three layers (need / product / offering + price history) built.
- Dashboard: matrix built; basket-by-store view next.
- Storage: local SQLite works; Bunny Database `kosik` seeded with the three-layer schema.

## Do NOT commit
`.env`, `.bunny/`, `prices.db`, `node_modules/`. All gitignored. If the DB token ever
leaks, rotate it in the Bunny dashboard.
