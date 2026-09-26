import * as esbuild from "npm:esbuild";
import { denoPlugins } from "jsr:@luca/esbuild-deno-loader";

await esbuild.build({
  plugins: [...denoPlugins({ configPath: new URL("./deno.json", import.meta.url).pathname })],
  entryPoints: ["./src/main.ts"],
  outfile: "./dist/index.ts",
  bundle: true,
  format: "esm",
});
esbuild.stop();
