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
