# CLAUDE.md — Košík grocery price tracker

Personal tool for one household (Dino, Prague 1). Tracks grocery prices across
Czech stores, shows the cheapest per item, and produces a shopping-list-by-store.
**Personal use only** — not a product. Never redistribute scraped prices.

## Current state (what works, what's stubbed)
- **DATABASE = Bunny Database (since 2026-09-23).** `pricetrack.py` talks to it directly
  over libSQL HTTP (`BunnyConn`: POST `<db>/v2/pipeline`, Bearer token from `.env`; bulk
  writes go as `batch` in BEGIN/COMMIT, 300 statements a request). All reads/writes, the
  server, fetch and crawl use Bunny. `prices.db` is only a fallback (`PRICETRACK_DB=local`);
  local data was copied up once with `migrate-to-bunny` (backup: `prices.backup-*.db`).
  Latency ~15-20 ms a call; matrix ~0.3 s.
- **Watchlist** (`watchlist.csv`, 32 items) = the original seed from a Makro receipt (EANs,
  VAT %, pack notes, `unit`/`unit_qty`). Items added later live only in the database.
- **Schema: three layers BUILT** (`bunny/schema.sql`, same SQL on Bunny and local):
  `store`, `need`, `product`, `offering`, `price`, plus views `v_offer` (resolved
  need/unit + latest price) and `v_matrix` (one cell per row×store, `is_cheapest`).
  `python3 pricetrack.py init` applies it (idempotent) to whichever backend is active.
- **Local server** (`python3 pricetrack.py serve` → localhost:8765, 127.0.0.1 only):
  matrix at `/`, **match screen at `/match.html`** (search Kupi/Rohlík per item, pick,
  remove, pack-size picker, refresh all). JSON API under `/api/` (matrix, products,
  search, link, unlink, fetch); writes need `Content-Type: application/json` + local Host.
  Stop-gap until the Edge Script read/write API exists.
- **`watch` table = what was matched** (source + ref → product/need, optional qty and
  Kupi `packs`). `fetch` walks watches, not offerings, so an item with no flyer this week
  stays tracked. Offerings a fetch no longer returns go `active=0` (their last price stops
  counting) and are revived if they come back.
- **CLI** (`pricetrack.py`): `init`, `seed`, `search <source>`, `match <source>` (to a
  product by EAN or to a need with `--need`), `fetch` (by cached id), `need add|link`,
  `store member <chain> on|off`, `matrix`, `export`.
- **SOURCE ≠ STORE.** A source is where prices come from (`offering.source`); a store is
  who sells. Rohlík is both. Hand-entered prices are `manual`.
- **DECIDED 2026-09-23: store prices come from the stores directly; Kupi is a separate
  FLYER-DEALS LAYER.** Store columns in the matrix = direct sources only (Rohlík now; more
  e-shops next). Kupi never writes offerings: a weekly full crawl (`crawl`) stores every
  current flyer offer of the tracked chains in `deal`; a kupi `watch` (slug → item) links
  deals to your items, and each matrix row shows its best current deal in a separate
  "flyer deal · kupi.cz" column, marked cheapest when it beats every store price.
- **Rohlík adapter: VERIFIED & WORKING** (`Rohlik` class in pricetrack.py; standalone
  `bunny/rohlik_probe.py`). See "Rohlík API" below. Rohlík has **no EAN search**.
- **Kupi adapter: VERIFIED & WORKING** (`Kupi` class). See "Kupi" below.
- **Makro adapter: VERIFIED & WORKING** (`Makro` class), no login: anonymous search gives
  the NET price per pack; VAT is added from the item's `vat_pct` (matches the receipt to the
  haléř). Matched per pack by EAN (`store_ref` = EAN). A live price for a store retires the
  hand-entered (`manual`) price for that store + item. Details: `docs/store-apis.md`.
- **Direct store connectors BUILT & VERIFIED 2026-09-23: Globus, Košík.cz, Tesco, Billa, dm,
  Rossmann, Teta, Makro, Rohlík** (one class each; endpoints in `docs/store-apis.md`).
  Lidl, Albert, Kaufland, Penny = flyer only (Kupi). Several prices for one listing (Tesco
  Clubcard, Rossmann Club / Rossmánek, Teta conditional promo + club) are separate `price`
  rows (`ref`), card ones with `club` set -> "requires card" in the UI.
- `.env` is loaded by pricetrack.py: `TESCO_XAPI_KEY` (Tesco's public web-app key, required
  for Tesco), optional `ROHLIK_COOKIE`, `MAKRO_STORE` (default 00005 Černý Most).
- **Barcode matches**: sources with `ean_search = True` (Globus, Košík, dm, Rossmann, Teta,
  Makro; Tesco via `ean_via_name`: name search filtered by gtin) are asked
  for an item's EAN when it's opened on the match screen (`/api/suggest`); exact hits get a
  one-click Link / Link all. Globus fuzzes EANs and Košík doesn't echo them back, so hits
  are exact-matched where possible and Košík's pack is shown for the user to check.
- Unlinking a store's live price brings back that store's hand-entered price.
- **Items can be added / edited / deleted in the UI** (match screen → "Add an item"; barcode
  lookup via Makro prefills name + EAN). New items live in the DB only; `watchlist.csv` is
  just the original seed.
- **Dashboard** (`dashboard/index.html`): the matrix (rows × stores, per-unit, cheapest
  marked, category filter) from `prices.json`. Basket view NOT built yet.

## The data model (THREE layers — important; built in bunny/schema.sql)

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

## Kupi.cz (VERIFIED live, 2026-09-23, no login, location Praha)
- **Aggregator of weekly/weekend chain flyers.** Promo prices only — no shelf prices.
- No JSON API; server-rendered HTML, plain GET works (no cookie, no bot wall).
  - search: `GET https://www.kupi.cz/hledej?f=<q>` → products as
    `<a href="/sleva/<slug>" title=… data-product-id="<n>">`
  - product: `GET https://www.kupi.cz/sleva/<slug>` → every current offer.
- Each offer = `<div class="discount_row" data-discount=<offer id> data-product=<n>
  data-key=<pack e.g. 250-g>>` with chain `/letaky/<chain>`, `.discount_price_value`,
  `.discount_amount` (pack), `.discounts_validity` text, optional `.discounts_club`
  (loyalty condition, e.g. "Cena s Kaufland Card XTRA") and `.discount_note` (limits).
  **Rows repeat across page sections → dedupe by offer id.**
- **One chain can have several offers for one product at once** (card vs regular price,
  hypermarket vs supermarket flyer). All are stored as separate `price` rows (`ref` =
  offer id); `v_current_price` picks the cheapest one valid today that you're eligible for.
  The user holds (or will hold) every chain's card, so card prices COUNT by default and are
  shown with a "requires card" note (`store.member` = 0 would exclude a chain's card prices).
- Validity text formats: `platí do úterý 29. 9.`, `čt 24. 9. – ne 27. 9.` (can start in
  the FUTURE), `dnes končí`, `zítra končí`. Stored as `valid_from` / `promo_until`;
  expired and not-yet-started offers drop out of the matrix automatically.
- One Kupi product can span pack sizes (Rama 225 g / 400 g) → offering per
  (chain, slug, pack): `store_ref = kupi:<slug>:<pack>`, unit_qty from the pack amount.
- Chain slug → store id mapping in `Kupi.CHAINS` (penny-market→penny,
  teta-drogerie→teta, dm-drogerie→dm); unknown chains (kosik-cz, jip, flop…) get created as
  stores with Kupi's name. Kupi also lists **Makro** flyers — unverified whether bez DPH.
- **Kupi lumps variants into one product** (Pampers Premium Care = all diaper sizes, as
  68/52/44 ks packs; "1-ks" = price per pack with no count). The match screen offers a
  size picker; the choice is stored in `watch.packs`.
- Match once per product (`match kupi <ean>` / `--need`, or the match screen).
- **Weekly crawl** (`pricetrack.py crawl`, or the button on the match screen): every page
  of `/slevy/<chain>` for `Kupi.CRAWL_CHAINS` (~20 grocery/drugstore chains, ~18 products a
  page, 1.5 s apart; Tesco alone ~50 pages; whole run ~10-20 min), then the product pages
  of watched items (catches chains outside the list). Category pages (`/slevy/<category>`)
  are NOT usable: they show only one offer per product.
- A deal counts while its date window covers today AND it was seen in its chain's latest
  crawl (`crawl_log`; chains outside the list use the last full run, `store_id='*'`).

## Other stores
Endpoints for every surveyed store are in `docs/store-apis.md` (Tesco's public API key is
in `.env` as `TESCO_XAPI_KEY`). For Heureka (price floor),
Makro (needs the user's session cookie; shows prices *bez DPH* — add VAT back using the
item's vat_pct): open the live site logged in, watch DevTools → Network for the real
search + price endpoints, capture the JSON shape, then write the adapter like Rohlík's.
Cache each store's product id per item (match once).

## Bunny setup (already done, for reference)
- `npm i -g @bunny.net/cli && bunny login`
- `bunny db create --name kosik --token --save-env` → writes `.env`
  (`BUNNY_DATABASE_URL`, `BUNNY_DATABASE_AUTH_TOKEN`). `.env` is gitignored.
- `python3 pricetrack.py init` applies schema + stores + watchlist to Bunny (`node
  bunny/seed.mjs` does the same from Node). JS client for Edge Scripts:
  `@bunny.net/database-client` (`connect()` reads the two env vars). SQLite dialect.
- The current token is a development token — rotate it when the build is done.

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
