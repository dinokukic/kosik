// One household password -> an HMAC-signed session cookie (WebCrypto, no dependencies).
import { env, must } from "./env.ts";

const COOKIE = "kosik";
const DAYS = 180;
const enc = new TextEncoder();
const b64url = (b: ArrayBuffer | Uint8Array) =>
  btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function hmac(data: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(must("SESSION_SECRET")),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", key, enc.encode(data)));
}

/** Constant-time string compare (via HMAC of both sides, so lengths don't leak either). */
async function same(a: string, b: string): Promise<boolean> {
  const [x, y] = await Promise.all([hmac("cmp:" + a), hmac("cmp:" + b)]);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.min(x.length, y.length); i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

export async function checkPassword(password: string): Promise<boolean> {
  const expected = env("APP_PASSWORD");
  return !!expected && !!password && await same(password, expected);
}

export async function sessionCookie(): Promise<string> {
  const exp = Math.floor(Date.now() / 1000) + DAYS * 86400;
  const payload = `v1.${exp}`;
  return `${COOKIE}=${payload}.${await hmac(payload)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${DAYS * 86400}`;
}
export const clearCookie = `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;

export async function isAuthed(req: Request): Promise<boolean> {
  const m = (req.headers.get("cookie") ?? "").match(new RegExp(`(?:^|;\\s*)${COOKIE}=([^;]+)`));
  if (!m) return false;
  const [v, exp, sig] = m[1].split(".");
  if (v !== "v1" || !exp || !sig || Number(exp) < Date.now() / 1000) return false;
  return await same(sig, await hmac(`v1.${exp}`));
}

/** Jobs (called by Mikku) authenticate with a bearer secret instead of the cookie. */
export async function isJobCall(req: Request): Promise<boolean> {
  const secret = env("JOB_SECRET");
  const got = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  return !!secret && !!got && await same(got, secret);
}
