// Load schema + watchlist into Bunny Database.
//   1) bunny db create --name kosik --token --save-env   (writes .env)
//   2) npm i @bunny.net/database-client
//   3) node bunny/seed.mjs
import { connect } from "@bunny.net/database-client";
import { readFileSync } from "node:fs";

// Auto-load .env from the project root (Node 20.12+ / 22). Falls back silently
// if there's no .env — you can also run: node --env-file=.env bunny/seed.mjs
try { process.loadEnvFile(new URL("../.env", import.meta.url)); } catch {}

if (!process.env.BUNNY_DATABASE_URL) {
  console.error(
    "\nNo BUNNY_DATABASE_URL found. Create the database first:\n" +
    "  bunny login\n" +
    "  bunny db create --name kosik --token --save-env\n" +
    "This writes BUNNY_DATABASE_URL + BUNNY_DATABASE_AUTH_TOKEN to .env, then re-run.\n" +
    "(Existing db? Get both from Dashboard -> Edge Platform -> Database -> Access,\n" +
    " put them in .env, or run: node --env-file=.env bunny/seed.mjs)\n");
  process.exit(1);
}

const db = connect(); // reads BUNNY_DATABASE_URL + BUNNY_DATABASE_AUTH_TOKEN

// 1) schema. Split on ";" is safe: schema.sql has no semicolons inside statements.
const schema = readFileSync(new URL("./schema.sql", import.meta.url), "utf8")
  .replace(/--.*$/gm, "");
for (const stmt of schema.split(";").map(s => s.trim()).filter(Boolean)) {
  await db.prepare(stmt).run();
}

// 2) stores (keep in sync with STORES in pricetrack.py)
const stores = [
  ["rohlik", "Rohlík", "https://www.rohlik.cz", 0],
  ["makro", "Makro", "https://sortiment.makro.cz", 1],
  ["kaufland", "Kaufland", "https://www.kaufland.cz", 0],
  ["tesco", "Tesco", "https://www.itesco.cz", 0],
  ["albert", "Albert", "https://www.albert.cz", 0],
  ["billa", "Billa", "https://www.billa.cz", 0],
  ["lidl", "Lidl", "https://www.lidl.cz", 0],
  ["penny", "Penny", "https://www.penny.cz", 0],
  ["globus", "Globus", "https://www.globus.cz", 0],
  ["dm", "dm", "https://www.dm.cz", 0],
  ["rossmann", "Rossmann", "https://www.rossmann.cz", 0],
  ["teta", "Teta", "https://www.tetadrogerie.cz", 0],
  ["alza", "Alza", "https://www.alza.cz", 0],
  ["rossmann_pl", "Rossmann PL", "https://www.rossmann.pl", 0],
  ["lindt", "Lindt", "https://www.lindt.cz", 0],
  ["heureka", "Heureka (min)", "https://www.heureka.cz", 0],
];
for (const s of stores) {
  await db.prepare(
    "INSERT INTO store (id,name,url,prices_ex_vat) VALUES (?,?,?,?) " +
    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url, " +
    "prices_ex_vat=excluded.prices_ex_vat"
  ).bind(...s).run();
}

// 3) watchlist -> product (unit + unit_qty are explicit columns in the CSV)
const csv = readFileSync(new URL("../watchlist.csv", import.meta.url), "utf8").trim().split("\n");
const cols = csv.shift().split(",");
let n = 0;
for (const line of csv) {
  const r = Object.fromEntries(line.split(",").map((v, i) => [cols[i], v]));
  await db.prepare(
    "INSERT INTO product (ean,name,category,vat_pct,unit,unit_qty,pack_note) " +
    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(ean) DO UPDATE SET name=excluded.name, " +
    "category=excluded.category, vat_pct=excluded.vat_pct, unit=excluded.unit, " +
    "unit_qty=excluded.unit_qty, pack_note=excluded.pack_note"
  ).bind(r.ean, r.name, r.category, Number(r.vat_pct) || null, r.unit || "pack",
         Number(r.unit_qty) || 1, r.pack_note || "").run();
  n++;
}
console.log(`seeded ${stores.length} stores + ${n} products into Bunny Database`);
