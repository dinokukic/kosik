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

// 1) schema
const schema = readFileSync(new URL("./schema.sql", import.meta.url), "utf8");
for (const stmt of schema.split(";").map(s => s.trim()).filter(Boolean)) {
  await db.prepare(stmt).run();
}

// 2) watchlist -> item
const csv = readFileSync(new URL("../watchlist.csv", import.meta.url), "utf8").trim().split("\n");
const cols = csv.shift().split(",");
let n = 0;
for (const line of csv) {
  const r = Object.fromEntries(line.split(",").map((v, i) => [cols[i], v]));
  await db.prepare(
    "INSERT INTO item (ean,name,category,vat_pct,pack_note) VALUES (?,?,?,?,?) " +
    "ON CONFLICT(ean) DO UPDATE SET name=excluded.name, category=excluded.category, " +
    "vat_pct=excluded.vat_pct, pack_note=excluded.pack_note"
  ).bind(r.ean, r.name, r.category, Number(r.vat_pct) || null, r.pack_note || "").run();
  n++;
}
console.log(`seeded ${n} items into Bunny Database`);
