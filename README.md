> **Claude-coded for personal use.**
>
> This talks to stores' own web APIs. Keep it personal and low-volume, respect each store's
> terms of use, and never redistribute the prices it collects.

# Košík: a household grocery price tracker

[![Deploy with Claude](https://img.shields.io/badge/Deploy%20with-Claude-D97757?logo=claude&logoColor=white)](#deploy-with-claude)

Košík answers one question before the weekly shop: **where is each thing on my list cheapest
this week?** It tracks the items you actually buy across ten Czech grocers, plus the weekly
flyers of the chains that don't sell online, and turns your picks into a printable shopping
list, one section per store.

It runs entirely on your own [bunny.net](https://bunny.net) account: one Edge Script serves the
app and its API, and a Bunny Database holds your items, prices and lists. No servers to run.

## What it does

- **Matrix.** Your items × stores, with each cell showing the shelf price, the pack size and
  the price per unit. The cheapest cell in each row is marked, and so is this week's best
  flyer deal.
- **Match.** Add an item once, search the stores (by barcode where a store supports it, by name
  where it doesn't) and link the right product at each one. From then on prices are fetched by
  id. Loose items can be grouped as a "need" (e.g. "chicken liver, any brand, per kg").
- **List.** Tap cells in the matrix to put items on the list at that store. The List page groups
  them by store with ticks and quantities, prints as a checklist, and can be saved and reused.
- **What your list costs.** A chart of your picks against the cheapest combination, the best
  two or three stores, the average and the most expensive. Tips like "only 2 items at dm: get
  them at Rossmann instead, +11 Kč, one stop fewer" come with an **Apply changes** button.
- **Weekly refresh.** Store prices, every current flyer offer (~12,000 a week from 19 chains via
  Kupi.cz) and barcode lookups, run in small slices so they fit Edge Script limits.

Prices are normalized per kg, litre, roll, egg or piece, so a six-pack and a single bottle
compare fairly. Loyalty-card prices are marked "requires card".

### Stores

| How | Stores |
|---|---|
| Direct, from the store's own shop | Globus, Košík.cz, Tesco (needs a key), Billa, dm, Rossmann, Teta, Makro, Rohlík |
| Weekly flyers via [Kupi.cz](https://www.kupi.cz) | Lidl, Albert, Kaufland, Penny and 15 more chains |

Endpoints and quirks for each store are in [docs/store-apis.md](docs/store-apis.md).

## Deploy with Claude

You need:
- a [bunny.net](https://bunny.net) account;
- [Claude Code](https://claude.com/claude-code);
- [Deno](https://deno.com) (`brew install deno`);
- the bunny CLI (`npm i -g @bunny.net/cli`).

1. **Log in to bunny.net.** This opens the browser; Claude never sees your API key.
   ```bash
   bunny login
   ```
2. **Clone the repo and start Claude with the deploy skill.**
   ```bash
   git clone https://github.com/dinokukic/kosik && cd kosik
   claude "/deploy-to-bunny"
   ```
   Saying "deploy this to my bunny.net account" works too.
3. **Answer Claude's questions:** a name for your resources (default `kosik`) and whether you
   want any of the [optional extras](#options). Claude then:
   - checks the prerequisites;
   - runs the setup script (about two minutes);
   - reports the URL.
4. **Open the URL and sign in.** The password is in `.env` as `KOSIK_PASSWORD`. Claude tells you
   where it is but doesn't print it.

### What gets created

The setup script ([edge/tools/setup.ts](edge/tools/setup.ts)) does, in order:
1. creates a Bunny Database and its tables and views;
2. creates an Edge Script with its pull zone;
3. pushes the settings, with passwords and keys stored as encrypted secrets (from a private temp
   file, never on the command line);
4. lets the login cookie through the pull zone (pull zones strip `Set-Cookie` by default);
5. builds and deploys;
6. signs in and calls the API to check it all works.

Each step is recorded in `.kosik-deploy.json`, so if something fails you fix it and run it
again, and finished steps are skipped. Edge Scripting and Bunny Database are billed by use,
and a household-sized setup costs cents a month.

### Without Claude

It's the same script:
```bash
bunny login
cd edge
deno task setup --name kosik     # prints the URL; the password goes to ../.env
```

### Options

Put any of these in `.env` in the repo root before you run the setup (create the file if it
doesn't exist; setup keeps what's already there):

| Variable | What it does | Without it |
|---|---|---|
| `KOSIK_PASSWORD` | Your login password | One is generated |
| `TESCO_XAPI_KEY` | Tesco's public web API key | Tesco is skipped |
| `MIKKU_API_KEY` | [mikku.dev](https://mikku.dev) runs the weekly refresh on Fridays by itself | Click **Refresh everything** on the Match page; it runs while the page is open |

**Custom domain.** Point a CNAME at your script's `*.bunny.run` hostname. Then run
`deno task setup --domain kosik.example.com`, which waits for DNS and issues SSL. Finally run
plain `deno task setup` once more.

### Updating and removing

```bash
git pull && cd edge && deno task setup      # redeploys the latest code
cd edge && deno task teardown               # deletes the script, pull zone and database, with all data
```

## Using it

**[The user guide](docs/guide/README.md)** walks through a week with Košík, with screenshots:
adding items (by barcode, by name, as generic items, flyer deals), refreshing prices, reading
the matrix, building the list, the cost comparison and tips, and printing.

In short:
1. **Match:** add the things you buy and link them at each store.
2. **Match → Refresh everything:** pull current prices and this week's flyers.
3. **Matrix:** see where everything is cheapest, then tap cells to build the list.
4. **List:** check the cost chart and tips, apply the ones you like, then print.

[![The matrix](docs/guide/img/matrix.png)](docs/guide/README.md)

## How it works

```
browser ──► Edge Script (pages + API, login cookie) ──► Bunny Database (SQL views do the grid)
                    │
                    └──► store APIs + Kupi.cz, in slices of ≤ 50 subrequests
                         (chained by Mikku, or driven by the open Match page)
```

- `edge/`: the Edge Script in TypeScript. `src/main.ts` routes, `src/stores.ts` holds the ten
  store connectors, `src/jobs.ts` the weekly run, `src/scenarios.ts` the list analysis.
- `dashboard/`: the pages (plain HTML/JS), embedded into the script at build time.
- `bunny/schema.sql`: the three-layer model (need / product / offering + price history) and
  the views behind the matrix.
- `pricetrack.py`: the original local tool (Python), still handy for scripting. See below.
- [CLAUDE.md](CLAUDE.md): the full design notes, written for Claude Code.

## Local tool (Python, optional)

Uses the same database. It needs `BUNNY_DATABASE_URL` and `BUNNY_DATABASE_AUTH_TOKEN` in `.env`
(`PRICETRACK_DB=local` uses a local SQLite file instead).
```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
python pricetrack.py serve                     # localhost:8765: matrix, match, list
python pricetrack.py match rohlik 8593803421216 --query "jihočeské máslo 250"
python pricetrack.py crawl                     # every current flyer offer
python pricetrack.py fetch                     # store prices of everything matched
```

## Never commit

`.env`, `.bunny/`, `.kosik-deploy.json`, `prices*.db`. All are gitignored. If a database token
ever leaks, rotate it in the bunny.net dashboard.
