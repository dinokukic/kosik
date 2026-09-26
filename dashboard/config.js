// Where the Košík API lives. Empty = same origin (local server, or the Edge Script's own
// domain). The static-site deploy overwrites this with the API's address.
window.KOSIK_API = "";
// Every API call: send the session cookie cross-origin too; not signed in -> the login page.
window.kosikFetch = async (path, init = {}) => {
  const r = await fetch(window.KOSIK_API + "api/" + path, { credentials: "include", ...init });
  if (r.status === 401) { location.href = "login.html"; throw new Error("not signed in"); }
  return r;
};
