-- Košík schema. SQLite dialect — shared by local prices.db (pricetrack.py) and
-- Bunny Database (bunny/seed.mjs). Three layers, see CLAUDE.md:
--
--   need      user's abstraction ("4-ply toilet paper"); basket = list of needs
--   product   same physical item across stores, joined by EAN when one exists
--   offering  one listing at one store (store's own product id) + price history
--
-- An offering belongs to a need either directly or through its product.
-- Matrix row key = need, else product, else the lone offering.

CREATE TABLE IF NOT EXISTS store (
  id            TEXT PRIMARY KEY,           -- 'rohlik', 'makro', 'dm', ...
  name          TEXT NOT NULL,
  url           TEXT,
  prices_ex_vat INTEGER NOT NULL DEFAULT 0, -- 1 = site shows bez DPH (Makro)
  member        INTEGER NOT NULL DEFAULT 1  -- 1 = you hold its loyalty card: club prices count
);

CREATE TABLE IF NOT EXISTS need (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  name     TEXT NOT NULL UNIQUE,
  category TEXT,
  unit     TEXT NOT NULL DEFAULT 'pack'     -- roll | egg | kg | l | piece | pack
);

CREATE TABLE IF NOT EXISTS product (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ean       TEXT UNIQUE,                    -- NULL when the item has no known EAN
  name      TEXT NOT NULL,
  category  TEXT,
  vat_pct   INTEGER,
  unit      TEXT NOT NULL DEFAULT 'pack',   -- unit to compare on (overrides need's when set)
  unit_qty  REAL NOT NULL DEFAULT 1,        -- units per pack: 16 rolls, 10 eggs, 0.25 kg
  pack_note TEXT,
  need_id   INTEGER REFERENCES need(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS offering (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id   TEXT NOT NULL REFERENCES store(id),
  store_ref  TEXT NOT NULL,                 -- source's id: rohlik '726539', kupi 'kupi:<slug>:<pack>',
                                            -- 'ean:<ean>' for hand-entered prices
  source     TEXT NOT NULL DEFAULT 'manual',-- where prices come from: rohlik | kupi | manual.
                                            -- Aggregators (kupi) write offerings under the CHAIN.
  name       TEXT NOT NULL,                 -- name as the store shows it
  product_id INTEGER REFERENCES product(id) ON DELETE SET NULL,
  need_id    INTEGER REFERENCES need(id) ON DELETE SET NULL,  -- when no shared product
  unit_qty   REAL,                          -- NULL = inherit product.unit_qty
  url        TEXT,
  active     INTEGER NOT NULL DEFAULT 1,
  UNIQUE (store_id, store_ref)
);

CREATE TABLE IF NOT EXISTS price (
  offering_id INTEGER NOT NULL REFERENCES offering(id) ON DELETE CASCADE,
  ts          TEXT NOT NULL,                -- ISO date captured
  ref         TEXT NOT NULL DEFAULT '',     -- source's offer id (kupi discount id); '' = the one price
  price_incl  REAL NOT NULL,                -- WITH vat (shelf-comparable)
  is_promo    INTEGER NOT NULL DEFAULT 0,
  valid_from  TEXT NOT NULL,                -- ISO date the price applies from (flyers can start later)
  promo_until TEXT,                         -- ISO date it ends; NULL = shelf price, no end
  club        TEXT,                         -- loyalty condition ('Cena s Kaufland Card XTRA'); NULL = anyone
  note        TEXT,                         -- limits etc. ('max 10 ks/osoba/den')
  PRIMARY KEY (offering_id, ts, ref)
);

-- What was matched: "source ref X is product/need Y". Survives weeks with no offers
-- (flyers rotate); fetch walks this, and offerings are (re)derived from it.
CREATE TABLE IF NOT EXISTS watch (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source     TEXT NOT NULL,                 -- rohlik | kupi
  ref        TEXT NOT NULL,                 -- rohlik product id, kupi slug
  name       TEXT,                          -- as the source calls it
  product_id INTEGER REFERENCES product(id) ON DELETE CASCADE,
  need_id    INTEGER REFERENCES need(id) ON DELETE CASCADE,
  qty        REAL,                          -- forced units per pack, NULL = from pack amount
  packs      TEXT                           -- kupi pack keys to keep ('68-ks,52-ks'), NULL = all
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_watch ON watch(source, ref, IFNULL(product_id, 0), IFNULL(need_id, 0));

-- Kupi flyer deals: a weekly crawl of every tracked chain's current flyer, kept apart
-- from store prices. Deals reach the matrix only through a kupi watch (slug -> item).
CREATE TABLE IF NOT EXISTS deal (
  ref          TEXT PRIMARY KEY,            -- kupi offer id (data-discount)
  slug         TEXT NOT NULL,               -- kupi product slug
  product_name TEXT,
  store_id     TEXT NOT NULL,               -- the chain selling it (never 'kupi')
  store_name   TEXT,
  pack_key     TEXT,                        -- '250-g', '68-ks', 'vse'
  amount       TEXT,                        -- '250 g'
  unit         TEXT,                        -- inferred from amount: kg | l | roll | piece...
  unit_qty     REAL,
  price_incl   REAL NOT NULL,
  valid_from   TEXT,                        -- NULL = already running
  valid_to     TEXT,                        -- NULL = no end given
  club         TEXT,                        -- loyalty condition; NULL = anyone
  note         TEXT,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deal_slug ON deal(slug);

-- Last completed crawl per chain: a deal not seen since then has left the flyer.
CREATE TABLE IF NOT EXISTS crawl_log (
  store_id TEXT PRIMARY KEY,
  ts       TEXT NOT NULL,
  deals    INTEGER
);

-- Shopping list: picked in the matrix (one store per row), printed as a to-do by store.
-- Price details are snapshotted when added; the list page also shows the current price.
CREATE TABLE IF NOT EXISTS cart_item (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  row_key     TEXT NOT NULL UNIQUE,         -- matrix row (need / product); one store per row
  row_name    TEXT,
  unit        TEXT,
  store_id    TEXT NOT NULL,
  store_name  TEXT,
  kind        TEXT NOT NULL DEFAULT 'store',-- store (a store's own price) | flyer (Kupi deal)
  item_name   TEXT,                         -- the store's name for it
  amount      TEXT,
  price_incl  REAL,                         -- pack price when added
  unit_price  REAL,
  promo_until TEXT,
  club        TEXT,
  note        TEXT,
  url         TEXT,
  qty         INTEGER NOT NULL DEFAULT 1,
  checked     INTEGER NOT NULL DEFAULT 0,
  added       TEXT NOT NULL
);

-- Barcode candidates for items without an EAN (name search at EAN-returning stores).
CREATE TABLE IF NOT EXISTS ean_candidate (
  product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  ean        TEXT NOT NULL,
  sources    TEXT,                          -- 'globus,tesco'
  name       TEXT,
  amount     TEXT,
  price      REAL,
  score      REAL,                          -- name overlap + pack size (+ both stores agree)
  PRIMARY KEY (product_id, ean)
);
CREATE TABLE IF NOT EXISTS ean_lookup (     -- which items were searched, and outcome
  product_id INTEGER PRIMARY KEY REFERENCES product(id) ON DELETE CASCADE,
  ts         TEXT NOT NULL,
  found      INTEGER,
  dismissed  INTEGER NOT NULL DEFAULT 0     -- user said: none of these
);

CREATE INDEX IF NOT EXISTS idx_offering_product ON offering(product_id);
CREATE INDEX IF NOT EXISTS idx_offering_need ON offering(need_id);
CREATE INDEX IF NOT EXISTS idx_price_ts ON price(ts);

-- The price that applies TODAY per offering: valid now, eligible (club prices only at stores
-- where store.member = 1), from the latest capture that has one, cheapest if several.
DROP VIEW IF EXISTS v_current_price;
CREATE VIEW v_current_price AS
WITH ok AS (
  SELECT pr.*, ROW_NUMBER() OVER (PARTITION BY pr.offering_id
                                  ORDER BY pr.ts DESC, pr.price_incl ASC) AS rn
  FROM price pr
  JOIN offering o ON o.id = pr.offering_id
  JOIN store s ON s.id = o.store_id
  WHERE pr.valid_from <= date('now')
    AND (pr.promo_until IS NULL OR pr.promo_until >= date('now'))
    AND (pr.club IS NULL OR s.member = 1)
)
SELECT offering_id, ts, ref, price_incl, is_promo, valid_from, promo_until, club, note
FROM ok WHERE rn = 1;

-- Every active offering with its resolved need/unit and today's price.
-- row_key groups offerings into one matrix row.
DROP VIEW IF EXISTS v_offer;
CREATE VIEW v_offer AS
SELECT
  o.id AS offering_id, o.store_id, o.source, o.store_ref, o.name AS offering_name, o.url,
  o.product_id, p.ean, p.name AS product_name, COALESCE(n.category, p.category) AS category,
  COALESCE(o.need_id, p.need_id) AS need_id, n.name AS need_name,
  CASE WHEN COALESCE(o.need_id, p.need_id) IS NOT NULL THEN 'n' || COALESCE(o.need_id, p.need_id)
       WHEN o.product_id IS NOT NULL THEN 'p' || o.product_id
       ELSE 'o' || o.id END AS row_key,
  COALESCE(n.name, p.name, o.name) AS row_name,
  COALESCE(n.unit, p.unit, 'pack') AS unit,
  COALESCE(o.unit_qty, p.unit_qty, 1) AS unit_qty,
  cp.ts, cp.price_incl, cp.is_promo, cp.promo_until, cp.club, cp.note,
  ROUND(cp.price_incl / COALESCE(o.unit_qty, p.unit_qty, 1), 2) AS unit_price
FROM offering o
LEFT JOIN product p ON p.id = o.product_id
LEFT JOIN need n ON n.id = COALESCE(o.need_id, p.need_id)
LEFT JOIN v_current_price cp ON cp.offering_id = o.id
WHERE o.active = 1;

-- Deals running today (and still in the chain's latest crawl), per watched item.
DROP VIEW IF EXISTS v_deal;
CREATE VIEW v_deal AS
SELECT
  d.*, w.product_id, COALESCE(w.need_id, p.need_id) AS need_id,
  CASE WHEN COALESCE(w.need_id, p.need_id) IS NOT NULL THEN 'n' || COALESCE(w.need_id, p.need_id)
       ELSE 'p' || w.product_id END AS row_key,
  COALESCE(n.name, p.name) AS row_name,
  COALESCE(n.category, p.category) AS category,
  COALESCE(n.unit, p.unit, 'pack') AS row_unit,
  ROUND(d.price_incl / COALESCE(w.qty,
          CASE WHEN d.unit = COALESCE(n.unit, p.unit) THEN d.unit_qty END,
          p.unit_qty, 1), 2) AS unit_price
FROM deal d
JOIN watch w ON w.source = 'kupi' AND w.ref = d.slug
  AND (w.packs IS NULL OR d.pack_key IS NULL
       OR instr(',' || w.packs || ',', ',' || d.pack_key || ',') > 0)
LEFT JOIN product p ON p.id = w.product_id
LEFT JOIN need n ON n.id = COALESCE(w.need_id, p.need_id)
LEFT JOIN crawl_log cl ON cl.store_id = d.store_id   -- this chain's last crawl
LEFT JOIN crawl_log g ON g.store_id = '*'            -- last full run (chains we don't crawl)
WHERE (d.valid_from IS NULL OR d.valid_from <= date('now'))
  AND (d.valid_to IS NULL OR d.valid_to >= date('now'))
  AND d.last_seen >= COALESCE(cl.ts, g.ts, d.last_seen);

-- Best flyer deal per matrix row.
DROP VIEW IF EXISTS v_row_deal;
CREATE VIEW v_row_deal AS
SELECT * FROM (
  SELECT v.*, ROW_NUMBER() OVER (PARTITION BY row_key ORDER BY unit_price, price_incl) AS rn
  FROM v_deal v
) WHERE rn = 1;

-- Matrix: one cell per (row, store) = that store's cheapest offering for the row,
-- compared on unit_price. is_cheapest marks the winning cell(s) per row.
DROP VIEW IF EXISTS v_matrix;
CREATE VIEW v_matrix AS
WITH ranked AS (
  SELECT v.*, ROW_NUMBER() OVER (PARTITION BY row_key, store_id ORDER BY unit_price) AS rn
  FROM v_offer v WHERE unit_price IS NOT NULL
)
SELECT row_key, row_name, category, unit, store_id, source, offering_id, offering_name,
       price_incl, unit_qty, unit_price, is_promo, promo_until, club, note, ts, url,
       unit_price = MIN(unit_price) OVER (PARTITION BY row_key) AS is_cheapest
FROM ranked WHERE rn = 1;
