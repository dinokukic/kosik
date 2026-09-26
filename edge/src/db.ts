// Bunny Database client. connect() does no I/O, so one client per isolate is fine.
import { connect } from "@bunny.net/database-client";
import { must } from "./env.ts";

// deno-lint-ignore no-explicit-any
export type Row = Record<string, any>;
let client: ReturnType<typeof connect> | null = null;

export function db() {
  // a hung query must not burn the whole invocation
  return client ??= connect({ url: must("BUNNY_DATABASE_URL"), authToken: must("BUNNY_DATABASE_AUTH_TOKEN"), timeout: 15000 });
}

/** Several read queries in one round trip (one transaction). */
export async function readMany(...queries: [string, unknown[]?][]): Promise<Row[][]> {
  const d = db();
  // deno-lint-ignore no-explicit-any
  const res = await d.batch(queries.map(([sql, args]) => d.prepare(sql).bind(...((args ?? []) as any[]))));
  return res.map((r) => r.rows as Row[]);
}

export async function all(sql: string, ...args: unknown[]): Promise<Row[]> {
  // deno-lint-ignore no-explicit-any
  return (await db().prepare(sql).bind(...(args as any[])).all()) as Row[];
}
export async function first(sql: string, ...args: unknown[]): Promise<Row | null> {
  // deno-lint-ignore no-explicit-any
  return (await db().prepare(sql).bind(...(args as any[])).first()) as Row | null;
}
export async function run(sql: string, ...args: unknown[]) {
  // deno-lint-ignore no-explicit-any
  return await db().prepare(sql).bind(...(args as any[])).run();
}
