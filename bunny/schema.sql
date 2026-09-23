-- Bunny Database is SQLite-compatible, so this is the same schema as local.
CREATE TABLE IF NOT EXISTS item (
  ean       TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  category  TEXT,
  vat_pct   INTEGER,
  pack_note TEXT
);
CREATE TABLE IF NOT EXISTS quote (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ean        TEXT NOT NULL,
  source     TEXT NOT NULL,
  price_incl REAL NOT NULL,     -- price WITH vat (shelf-comparable)
  is_promo   INTEGER DEFAULT 0,
  url        TEXT,
  ts         TEXT NOT NULL      -- ISO date the quote was captured
);
-- one quote per (ean, source, day)
CREATE UNIQUE INDEX IF NOT EXISTS uq_quote_day ON quote(ean, source, ts);
CREATE INDEX IF NOT EXISTS idx_quote_ean_ts ON quote(ean, ts);
