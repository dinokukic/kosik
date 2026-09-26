// One-command deploy of Košík to your own bunny.net account (and the matching teardown).
//
//   deno task setup [--name kosik] [--domain api.example.com]
//   deno task teardown [--yes]
//
// Needs the bunny CLI, logged in (`npm i -g @bunny.net/cli && bunny login`). Creates, in order:
// a Bunny Database (+ schema), an Edge Script with its pull zone, the script's settings and
// secrets, and deploys the code; the script serves the pages itself, so there's nothing else to
// host. Safe to re-run: finished steps are recorded in .kosik-deploy.json and skipped.
//
// Optional, read from the environment or ../.env:
//   KOSIK_PASSWORD   the login password (generated and saved to .env if missing)
//   TESCO_XAPI_KEY   Tesco's web API key (without it Tesco is skipped)
//   MIKKU_API_KEY    mikku.dev key: runs the weekly refresh on Fridays by itself (without it,
//                    "Refresh everything" on the Match page runs it while the page is open)
// Secrets never go on a command line or into the output: they're pushed from a private temp file.

const ROOT = new URL("../../", import.meta.url), EDGE = new URL("../", import.meta.url);
const STATE = new URL(".kosik-deploy.json", ROOT), DOTENV = new URL(".env", ROOT);
const args = Deno.args;
const flag = (name: string) => { const i = args.indexOf(`--${name}`); return i >= 0 ? (args[i + 1] ?? "") : undefined; };
const has = (name: string) => args.includes(`--${name}`);

type State = { name?: string; db?: { id: string; url: string }; schema?: boolean;
  script?: { id: number; pullZone: number; host: string }; env?: boolean; cookies?: boolean; deployed?: boolean; domain?: string };
const load = async (): Promise<State> => JSON.parse(await Deno.readTextFile(STATE).catch(() => "{}"));
const save = (s: State) => Deno.writeTextFile(STATE, JSON.stringify(s, null, 2) + "\n");

// ---- .env (read + merge, never printed)
async function readEnv(): Promise<Record<string, string>> {
  const txt = await Deno.readTextFile(DOTENV).catch(() => "");
  return Object.fromEntries([...txt.matchAll(/^([A-Z0-9_]+)=(.*)$/gm)].map((m) => [m[1], m[2].trim().replace(/^["']|["']$/g, "")]));
}
async function writeEnv(vals: Record<string, string>) {
  let txt = await Deno.readTextFile(DOTENV).catch(() => "");
  for (const [k, v] of Object.entries(vals)) {
    const line = `${k}=${v}`;
    txt = new RegExp(`^${k}=.*$`, "m").test(txt) ? txt.replace(new RegExp(`^${k}=.*$`, "m"), line) : `${txt.replace(/\n?$/, "\n")}${line}\n`;
  }
  await Deno.writeTextFile(DOTENV, txt, { mode: 0o600 });
}

// ---- the bunny CLI
async function bunny(cmd: string[], opts: { cwd?: URL; json?: boolean } = {}) {
  const out = await new Deno.Command("bunny", { args: opts.json ? [...cmd, "--output", "json"] : cmd,
    cwd: opts.cwd ?? ROOT, stdout: "piped", stderr: "piped", env: { NO_COLOR: "1" } }).output();
  const stdout = new TextDecoder().decode(out.stdout), stderr = new TextDecoder().decode(out.stderr);
  if (!out.success) throw new Error(`bunny ${cmd.slice(0, 3).join(" ")} failed:\n${(stderr || stdout).trim().slice(0, 800)}`);
  if (!opts.json) return stdout;
  try { return JSON.parse(stdout.slice(stdout.search(/[[{]/))); }
  catch { throw new Error(`bunny ${cmd.slice(0, 3).join(" ")}: expected JSON, got: ${stdout.slice(0, 300)}`); }
}
/** Find the first string value matching a test anywhere in a JSON value. */
// deno-lint-ignore no-explicit-any
function find(v: any, test: (s: string) => boolean): string | undefined {
  if (typeof v === "string") return test(v) ? v : undefined;
  if (v && typeof v === "object") for (const x of Object.values(v)) { const r = find(x, test); if (r) return r; }
}
const secret = (bytes = 24) => btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes)))).replace(/[+/=]/g, "").slice(0, 32);
const step = (msg: string) => console.log(`\n→ ${msg}`);
const ok = (msg: string) => console.log(`  ✓ ${msg}`);

async function run(cmd: string[], cwd: URL) {
  const r = await new Deno.Command(cmd[0], { args: cmd.slice(1), cwd, stdout: "piped", stderr: "piped" }).output();
  if (!r.success) throw new Error(`${cmd.join(" ")} failed:\n${new TextDecoder().decode(r.stderr).slice(0, 800)}`);
}

// ======================================================================= setup
async function setup() {
  const st = await load();
  const env = { ...(await readEnv()), ...Deno.env.toObject() };
  const name = st.name ?? flag("name") ?? "kosik";
  if (!/^[a-z0-9-]{3,40}$/.test(name)) throw new Error("--name: 3-40 lowercase letters, digits or dashes");
  st.name = name; await save(st);

  step("Checking the bunny CLI login");
  await bunny(["api", "GET", "/user"]).catch(() => { throw new Error("Not logged in. Run `bunny login` first (it opens the browser)."); });
  ok("logged in");

  // 1. database
  if (!st.db) {
    const mine = (await readEnv()).BUNNY_DATABASE_URL;
    if (mine) throw new Error(".env already points at a database (BUNNY_DATABASE_URL). Remove those lines or deploy from a fresh clone.");
    step(`Creating the database "${name}"`);
    const d = await bunny(["db", "create", "--name", name, "--mode", "auto", "--no-link", "--token", "--no-save-env"], { json: true });
    const url = find(d, (s) => s.startsWith("libsql://"));
    const id = find(d, (s) => /^db_[0-9A-Z]+$/.test(s));
    let token = find(d, (s) => s.startsWith("eyJ"));
    if (!url || !id) throw new Error(`unexpected db create output (keys: ${Object.keys(d).join(", ")})`);
    token ??= find(await bunny(["db", "tokens", "create", id, "--no-save", "--force"], { json: true }), (s) => s.startsWith("eyJ"));
    if (!token) throw new Error("could not get a database token");
    await writeEnv({ BUNNY_DATABASE_URL: url, BUNNY_DATABASE_AUTH_TOKEN: token });
    st.db = { id, url }; await save(st);
    ok(`database ${id} (credentials saved to .env)`);
  }
  if (!st.schema) {
    step("Creating the tables and views");
    // no id: the CLI takes this database's URL + token from ../.env (keeps the token off the command line)
    await bunny(["db", "shell", "-e", new URL("bunny/schema.sql", ROOT).pathname]);
    st.schema = true; await save(st);
    ok("schema applied");
  }

  // 2. edge script + pull zone
  if (!st.script) {
    step(`Creating the Edge Script "${name}"`);
    const c = await bunny(["scripts", "create", name, "--type", "standalone", "--pull-zone", "--link"], { cwd: EDGE, json: true });
    const id = Number(c.Id ?? c.id ?? find(c, (s) => /^\d+$/.test(s)));
    const s = await bunny(["scripts", "show", String(id)], { json: true });
    const pz = s.LinkedPullZones?.[0];
    if (!id || !pz) throw new Error("could not read the new script's id / pull zone");
    st.script = { id, pullZone: pz.Id, host: String(pz.DefaultHostname ?? s.DefaultHostname).replace(/\/$/, "") };
    await save(st);
    ok(`script ${id} at ${st.script.host}`);
  }
  const base = st.domain ? `https://${st.domain}` : st.script.host;

  // 3. settings + secrets
  if (!st.env) {
    step("Setting the script's settings and secrets");
    const cur = await readEnv();
    const password = env.KOSIK_PASSWORD || cur.KOSIK_PASSWORD || secret(12);
    const jobSecret = cur.KOSIK_JOB_SECRET || secret();
    await writeEnv({ KOSIK_PASSWORD: password, KOSIK_JOB_SECRET: jobSecret, KOSIK_API: `${base}/` });
    const vals: Record<string, string> = {
      BUNNY_DATABASE_URL: cur.BUNNY_DATABASE_URL, BUNNY_DATABASE_AUTH_TOKEN: cur.BUNNY_DATABASE_AUTH_TOKEN,
      APP_PASSWORD: password, SESSION_SECRET: secret(), JOB_SECRET: jobSecret, API_URL: base, SITE_URL: base,
    };
    if (env.TESCO_XAPI_KEY) vals.TESCO_XAPI_KEY = env.TESCO_XAPI_KEY;
    if (env.MIKKU_API_KEY) vals.MIKKU_API_KEY = env.MIKKU_API_KEY;
    const plain = ["API_URL", "SITE_URL"], secrets = Object.keys(vals).filter((k) => !plain.includes(k));
    const tmp = await Deno.makeTempFile({ suffix: ".env" });
    try {
      await Deno.chmod(tmp, 0o600);
      await Deno.writeTextFile(tmp, Object.entries(vals).map(([k, v]) => `${k}=${v}`).join("\n") + "\n");
      await bunny(["scripts", "env", "push", tmp, "--id", String(st.script.id), "--all",
        "--secrets", secrets.join(","), "--plain", plain.join(",")], { cwd: EDGE });
    } finally { await Deno.remove(tmp).catch(() => {}); }
    st.env = true; await save(st);
    ok(`${Object.keys(vals).length} settings (${secrets.length} as encrypted secrets)` +
      (vals.TESCO_XAPI_KEY ? "" : " · no TESCO_XAPI_KEY: Tesco skipped") + (vals.MIKKU_API_KEY ? "" : " · no MIKKU_API_KEY: refresh from the Match page"));
  }

  // 4. pull zones strip Set-Cookie by default, which would break the login
  if (!st.cookies) {
    step("Letting the login cookie through the pull zone");
    await bunny(["api", "POST", `/pullzone/${st.script.pullZone}`, "--body", JSON.stringify({ DisableCookies: false })]);
    st.cookies = true; await save(st);
    ok("DisableCookies off");
  }

  // 5. build + deploy (settings are read at deploy time, so this comes after them)
  step("Building and deploying");
  await run(["deno", "task", "build"], EDGE);
  await bunny(["scripts", "deploy", "dist/index.ts", String(st.script.id)], { cwd: EDGE });
  st.deployed = true; await save(st);
  ok("deployed");

  // 6. optional custom domain
  const domain = flag("domain");
  if (domain && st.domain !== domain) {
    step(`Adding ${domain} (point a CNAME at ${st.script.host.replace("https://", "")} first; waits up to 10 min)`);
    await bunny(["scripts", "domains", "add", domain, String(st.script.id), "--wait"], { cwd: EDGE });
    st.domain = domain; st.env = false; await save(st);
    ok(`${domain} added; run \`deno task setup\` once more to point the settings at it`);
  }

  // 7. check it works: the page, the login cookie, the API
  step("Checking the deployment");
  const password = (await readEnv()).KOSIK_PASSWORD;
  let cookie = "";
  for (let i = 0; i < 12 && !cookie; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    const r = await fetch(`${base}/api/login`, { method: "POST", redirect: "manual",
      body: new URLSearchParams({ password }), headers: { "Content-Type": "application/x-www-form-urlencoded" } }).catch(() => null);
    await r?.body?.cancel();
    cookie = r?.headers.get("set-cookie")?.split(";")[0] ?? "";
  }
  if (!cookie) throw new Error("login didn't return a session cookie (deploys can take a minute; re-run `deno task setup`)");
  const m = await fetch(`${base}/api/matrix`, { headers: { cookie } });
  if (!m.ok) throw new Error(`/api/matrix answered ${m.status}: ${(await m.text()).slice(0, 200)}`);
  await m.body?.cancel();
  ok("login and API work");

  if (env.MIKKU_API_KEY) {
    step("Scheduling the weekly refresh on Mikku (Fridays 13:00 UTC)");
    const r = await fetch(`${base}/jobs/setup`, { method: "POST", headers: { Authorization: `Bearer ${(await readEnv()).KOSIK_JOB_SECRET}` } });
    await r.body?.cancel();
    if (!r.ok) throw new Error(`/jobs/setup answered ${r.status}`);
    ok("scheduled");
  }

  console.log(`\nDone. Open ${base}/ and sign in with the password in .env (KOSIK_PASSWORD).
Start on the Match page: add an item, search the stores, link the right products.`);
}

// ======================================================================= teardown
async function teardown() {
  const st = await load();
  if (!st.db && !st.script) { console.log("Nothing to remove (.kosik-deploy.json is empty)."); return; }
  const what = [st.script && `Edge Script ${st.script.id} + pull zone ${st.script.pullZone}`, st.db && `database ${st.db.id}`].filter(Boolean).join(", ");
  if (!has("yes") && !confirm(`Permanently delete ${what} and all its data?`)) return;
  if (st.script) {
    step(`Deleting Edge Script ${st.script.id}`);
    await bunny(["scripts", "delete", String(st.script.id), "--force"], { cwd: EDGE });
    await bunny(["api", "DELETE", `/pullzone/${st.script.pullZone}`]).catch((e) => console.log(`  (pull zone: ${e.message.split("\n")[0]})`));
    await Deno.remove(new URL(".bunny/script.json", EDGE)).catch(() => {});
    delete st.script; st.env = st.cookies = st.deployed = false; await save(st);
    ok("script and pull zone deleted");
  }
  if (st.db) {
    step(`Deleting database ${st.db.id}`);
    await bunny(["db", "delete", st.db.id, "--force"]);
    delete st.db; st.schema = false; await save(st);
    ok("database deleted");
  }
  await Deno.remove(STATE).catch(() => {});
  console.log("\nDone. .env still holds the old values; delete it if you don't need them.");
}

try {
  await (args[0] === "teardown" ? teardown() : setup());
} catch (e) {
  console.error(`\n✗ ${(e as Error).message}`);
  Deno.exit(1);
}
