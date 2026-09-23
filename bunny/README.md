# Bunny Database backend

Bunny Database is a globally-distributed, **SQLite-compatible** service (libSQL).
Our schema ports over unchanged. Set-up is done with the Bunny CLI.

## 1. Install the CLI & log in
```bash
npm install -g @bunny.net/cli
bunny login
```

## 2. Create the database (writes creds to .env)
```bash
bunny db create --name kosik --token --save-env
# -> writes BUNNY_DATABASE_URL and BUNNY_DATABASE_AUTH_TOKEN to .env
```
(Or grab them from Dashboard → Edge Platform → Database → your db → Access.)

## 3. Create tables + load the watchlist
```bash
npm i @bunny.net/database-client
node bunny/seed.mjs
```
Sanity-check in the SQL shell:
```bash
bunny db shell
> SELECT count(*) FROM product;
```

## What's next (not built yet)
- **pull-job.ts** — an Edge Script that fetches prices for matched offerings and writes `price` rows.
  Deploy with `bunny scripts init` / `bunny scripts deploy`; Edge Scripting
  auto-injects BUNNY_DATABASE_URL/_AUTH_TOKEN. Trigger its URL from any external
  cron (e.g. weekly).
- **api.ts** — a second Edge Script that runs the read query and returns the
  dashboard JSON, so the token stays server-side and the browser only sees results.

Server-side only: the auth token grants full DB access and the client sends raw
SQL — never ship it to the browser.
