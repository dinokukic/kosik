---
name: deploy-to-bunny
description: Deploy Košík to the user's own bunny.net account (database, Edge Script, settings, login) with one scripted setup, or remove such a deployment. Use when the user asks to deploy, install, set up or host Košík, or to tear a deployment down.
---

# Deploy Košík to bunny.net

Everything is scripted in `edge/tools/setup.ts` (`deno task setup` / `deno task teardown`, run
from `edge/`). Your job is to get the prerequisites right, run it, and explain the result. The
script is idempotent: if a step fails, fix the cause and run it again; finished steps are skipped
(progress lives in `.kosik-deploy.json`).

## 1. Prerequisites (check, don't assume; run from the repo root)
- `deno --version` — if missing, suggest `brew install deno` (macOS) or https://deno.com and ask
  before installing anything.
- `bunny --version` — if missing, suggest `npm i -g @bunny.net/cli`; ask before installing.
- `bunny api GET /user` — if it fails, the user must run `bunny login` themselves (it opens the
  browser). Never ask for or handle their bunny.net API key.
- `.env` in the repo root must not already have `BUNNY_DATABASE_URL` (the script refuses, so an
  existing database is never overwritten). If it does, ask whether this is an existing
  deployment (then just re-run setup) or whether they want a fresh clone.

## 2. Ask the user (one message)
- A name for their resources (default `kosik`; lowercase, digits, dashes).
- Optional extras. They add them to `.env` in the repo root themselves (create it if it doesn't
  exist; setup keeps any keys already in it); don't ask them to paste keys into the chat:
  - `TESCO_XAPI_KEY`: Tesco's public web API key. Without it Tesco is skipped.
  - `MIKKU_API_KEY` (mikku.dev): runs the weekly refresh by itself on Fridays. Without it,
    "Refresh everything" on the Match page does the same while the page stays open.
  - `KOSIK_PASSWORD`: their chosen login password. Otherwise one is generated.
- Optional custom domain (e.g. `kosik.example.com`). It needs a CNAME to the script's
  `*.bunny.run` hostname first, so it's easiest to deploy without one and add it after.

## 3. Run it
```bash
cd edge && deno task setup --name <name>
```
It prints nothing for a minute or two while it builds and deploys: use a long Bash timeout
(10 min) and tell the user it's working. Its last step signs in and calls the API, so a ✓ there
means the deployment works; you don't need your own login check.

Re-runs remember the name (`.kosik-deploy.json`), so `--name` is only needed the first time.
Custom domain: once the CNAME exists, run `deno task setup --domain <domain>` (waits for DNS,
issues SSL), then plain `deno task setup` once more so the settings and the check use it.
The URL is in `.kosik-deploy.json` (`script.host`, or `domain`) if you need it again.

The script: creates the database and its schema → creates the Edge Script and its pull zone →
pushes settings (secrets from a private temp file, never on the command line) → allows cookies
on the pull zone (it strips `Set-Cookie` by default, which breaks login) → builds and deploys →
signs in and calls the API to check → schedules Mikku if there's a key.

## 4. Report
- The URL to open (printed at the end).
- **The password is in `.env` as `KOSIK_PASSWORD`.** Tell them where it is, but never print
  `.env` or any secret into the chat.
- What to do next: Match page → add an item → search the stores → link the right products;
  then Matrix and List.
- What it costs: Edge Scripting and Bunny Database are billed by use; a household-sized setup
  stays in cents per month.

## Removing it
Only when the user asks: `cd edge && deno task teardown` (asks for confirmation; `--yes` skips
it). This permanently deletes the script, its pull zone and the database with all its data.
Confirm with the user in chat first and name what will be deleted.

## Rules
- Don't print `.env`, tokens, passwords or `bunny scripts show` output (it contains the script's
  deployment key); filter any JSON you inspect down to the fields you need.
- Don't delete or overwrite resources that aren't in `.kosik-deploy.json`.
- Scraping stays personal and low-volume; never redistribute prices.
