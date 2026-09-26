// Static site build: ../dashboard -> dist-site/, with config.js pointing at the API domain.
const API = Deno.env.get("KOSIK_API") ?? "https://api.kosik.ulandino.com/";
const src = new URL("../../dashboard/", import.meta.url), out = new URL("../dist-site/", import.meta.url);
await Deno.remove(out, { recursive: true }).catch(() => {});
await Deno.mkdir(out, { recursive: true });
for await (const e of Deno.readDir(src)) {
  if (e.isFile && /\.(html|css|js)$/.test(e.name)) await Deno.copyFile(new URL(e.name, src), new URL(e.name, out));
}
const cfg = await Deno.readTextFile(new URL("config.js", out));
await Deno.writeTextFile(new URL("config.js", out), cfg.replace('window.KOSIK_API = "";', `window.KOSIK_API = ${JSON.stringify(API)};`));
console.log("dist-site ready, API =", API);
