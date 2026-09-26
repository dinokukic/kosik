// Edge Scripting exposes variables/secrets on process.env; Deno.env when running locally.
export function env(name: string): string | undefined {
  // deno-lint-ignore no-explicit-any
  const g = globalThis as any;
  return g.process?.env?.[name] ?? g.Deno?.env?.get?.(name);
}
export function must(name: string): string {
  const v = env(name);
  if (!v) throw new Error(`missing env ${name}`);
  return v;
}
