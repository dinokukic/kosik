// Static site build: ../dashboard -> dist-site/, with config.js pointing at the API domain.
// KOSIK_API = the Edge Script's public URL, from the environment or ../.env (e.g. https://api.kosik.example.com/)
const dotenv = await Deno.readTextFile(new URL("../../.env", import.meta.url)).catch(() => "");
const API = Deno.env.get("KOSIK_API") ?? dotenv.match(/^KOSIK_API=(.*)$/m)?.[1]?.trim().replace(/^["']|["']$/g, "");
if (!API) { console.error("Set KOSIK_API (the API's URL, e.g. https://api.kosik.example.com/) in the environment or .env"); Deno.exit(1); }
const src = new URL("../../dashboard/", import.meta.url), out = new URL("../dist-site/", import.meta.url);
await Deno.remove(out, { recursive: true }).catch(() => {});
await Deno.mkdir(out, { recursive: true });
for await (const e of Deno.readDir(src)) {
  if (e.isFile && /\.(html|css|js)$/.test(e.name)) await Deno.copyFile(new URL(e.name, src), new URL(e.name, out));
}
const cfg = await Deno.readTextFile(new URL("config.js", out));
await Deno.writeTextFile(new URL("config.js", out), cfg.replace('window.KOSIK_API = "";', `window.KOSIK_API = ${JSON.stringify(API)};`));

// Assets are cached for a day by browsers: give each a content hash so a deploy is seen at once.
const hash = async (name: string) => {
  const d = await crypto.subtle.digest("SHA-256", await Deno.readFile(new URL(name, out)));
  return [...new Uint8Array(d)].slice(0, 5).map((b) => b.toString(16).padStart(2, "0")).join("");
};
const v = { "style.css": await hash("style.css"), "config.js": await hash("config.js") };
for await (const e of Deno.readDir(out)) {
  if (!e.name.endsWith(".html")) continue;
  const f = new URL(e.name, out);
  await Deno.writeTextFile(f, (await Deno.readTextFile(f))
    .replace(/(href|src)="(style\.css|config\.js)"/g, (_, a, n: keyof typeof v) => `${a}="${n}?v=${v[n]}"`));
}
console.log("dist-site ready, API =", API, v);
