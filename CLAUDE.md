# CLAUDE.md — Košík grocery price tracker

Personal tool for one household (Dino, Prague 1). Tracks grocery prices across
Czech stores, shows the cheapest per item, and produces a shopping-list-by-store.
**Personal use only** — not a product. Never redistribute scraped prices.

## Current state (what works, what's stubbed)
- **Watchlist** (`watchlist.csv`, 32 items) seeded from a real Makro receipt with
  EANs, VAT %, and pack notes. Source of truth for items.
- **Storage**: local SQLite (`pricetrack.py`) AND Bunny Database (SQLite-compatible,
  Public Preview). Bunny is the target. DB `kosik` already created in Prague region;
  schema + watchlist seeded via `bunny/seed.mjs`.
- **Rohlík adapter: VERIFIED & WORKING** (`bunny/rohlik_probe.py` + `src_rohlik` in
  pricetrack.py). See "Rohlík API" below. Rohlík has **no EAN search**.
- **Kupi / Heureka / Makro adapters: UNVERIFIED SCAFFOLDS** — endpoints/selectors are
  guesses. Each needs the same live-inspect-and-wire treatment Rohlík got (use Claude
  in Chrome / DevTools Network tab against the logged-in live site).
- **Dashboard** (`dashboard/index.html`): store-agnostic list, cheapest-per-item vs
  30-day average. This is a PLACEHOLDER for the real matrix view (see below).

## The data model to build (THREE layers — important)
The current schema has only `item` + `quote`. Refactor to three layers:

1. **offering** — a specific product at a specific store (e.g. Makro's Harmony 16-roll
   4-ply), with price history. ~= today's `quote`, but tied to a per-store product ref.
2. **product** — the same real item across stores, joined by **EAN when it exists**;
   where a store has no EAN search (Rohlík), cache that store's own product id.
   "Compare the exact same thing everywhere." Optional per item.
3. **need / slot** — the user's abstraction: "4-ply toilet paper", "free-range M eggs",
   "beef mince". Groups many offerings that satisfy the same need and compares them on a
   normalized unit. **The basket is a list of needs.** User fills these MANUALLY (drag
   offerings into a slot); EAN can *suggest* "these look identical, link them?".

Why: Makro's own 4-ply and Rossmann's exclusive 4-ply have different EANs/brands but the
user only cares about "cheapest 4-ply". Needs collapse near-duplicate rows into one
comparable row. Without this the matrix is a wall of duplicates.

## Unit normalization (keep SIMPLE)
Compute price per obvious unit: per roll, per egg, per kg (weight items), per pack.
NO per-sheet / per-100-sheet cleverness for TP — roll length isn't reliably published
and the user doesn't care. Infer a sensible unit from category; allow override.

## Main dashboard = a MATRIX
- Rows = products / needs (a need like "4-ply TP" is one row spanning brands).
- Columns = stores.
- Cell = that store's current price for that product (per-unit where relevant).
- **The cheapest cell per row is always visually marked.**
- The matrix shows TRUTH (cheapest everywhere). It does NOT auto-decide.

## Basket = shopping-list-by-store (manual, not an optimizer)
- User picks which stores they'll actually visit this week.
- For each need, show the cheapest offering **at each store being visited**.
- Running total per store + overall. User can drop a store and knowingly take a
  slightly worse price to save a trip ("not driving to dm for one item").
- The matrix informs; the basket reflects the user's real trip choices.

## Add-a-product flow (a first-class feature)
Typing a new product SEARCHES each store from inside the tool, shows candidates per
store, user picks the right one per store, saves. Adding an item == resolving its
per-store product ids == the matching step. Add-item UI and match UI are the same screen.

## Rohlík API (VERIFIED live, 2026-09-23, logged in, Prague 1)
- **No EAN search.** Searching an EAN returns "nothing found". Must search by NAME.
- Step 1 — name → product ids:
  `GET https://www.rohlik.cz/services/frontend-service/autocomplete?search=<q>&referer=whisperer&companyId=1`
  → `{ "productIds": [1480199, ...], ... }`
- Step 2 — ids → price/promo (VAT-inclusive):
  `GET https://www.rohlik.cz/api/v1/products/card?products=<id>&products=<id>&categoryType=normal`
  → `[{ productId, name, textualAmount, badges,
        prices:{ originalPrice, salePrice, unitPrice, saleId, saleValidTill, currency }, stock }]`
  Pay price = `salePrice ?? originalPrice`; promo when `salePrice < originalPrice`.
- **MATCHING IS THE HARD PART**: one name search returns the whole family (sizes 1–6,
  pants…). You CANNOT auto-pick reliably. Resolve the correct `productId` once per item
  (user confirms the right pack) and CACHE it. Then fetch by id forever after.
- **Pin prices to Prague 1**: pass the user's logged-in session via `ROHLIK_COOKIE`
  (prices vary by delivery region; Prague 1 is the intended region).

## Other stores — same recipe, still to do
For Kupi.cz (flyer promos, aggregates chains — highest value), Heureka (price floor),
Makro (needs the user's session cookie; shows prices *bez DPH* — add VAT back using the
item's vat_pct): open the live site logged in, watch DevTools → Network for the real
search + price endpoints, capture the JSON shape, then write the adapter like Rohlík's.
Cache each store's product id per item (match once).

## Bunny setup (already done, for reference)
- `npm i -g @bunny.net/cli && bunny login`
- `bunny db create --name kosik --token --save-env` → writes `.env`
  (`BUNNY_DATABASE_URL`, `BUNNY_DATABASE_AUTH_TOKEN`). `.env` is gitignored.
- `node bunny/seed.mjs` (auto-loads `.env`; also `node --env-file=.env`) loads schema+items.
- Client: `@bunny.net/database-client` (`connect()` reads the two env vars). SQLite dialect.

## Target architecture (end state)
- **Pull job** = a Bunny **Edge Script** (TS) that fetches prices and writes `offering`
  history rows. Edge Scripting auto-injects the DB env vars. Trigger its URL from an
  external cron (weekly). Edge scripts are Deno + time-limited → keep each run light
  (one source or a small batch), fail soft per source.
- **Read API** = a second Edge Script that runs the matrix/basket queries and returns
  JSON, so the DB token stays server-side (NEVER ship the token to the browser).
- **Dashboard** = static page (host on Bunny) that fetches from the read API.

## House rules
- Secrets never in git or the browser. `.env` + `.bunny/` are gitignored. If a token
  leaks, rotate it (Dashboard → Edge Platform → Database → kosik → Access).
- Scraping is personal & low-volume: real delays between requests, cache aggressively,
  fail soft. Respect each site.
- Prices are stored WITH VAT (shelf-comparable). Makro is the one source that needs VAT
  added back.
