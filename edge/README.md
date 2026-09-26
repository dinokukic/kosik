# Košík on bunny.net Edge Scripting

The Košík API + pages as one Edge Script (`kosik-api`), talking to Bunny Database.

```bash
deno task build     # embed ../dashboard pages, bundle to dist/index.ts
deno task deploy    # build + bunny scripts deploy
```

Secrets (bunny scripts env set NAME --secret): BUNNY_DATABASE_URL, BUNNY_DATABASE_AUTH_TOKEN,
APP_PASSWORD, SESSION_SECRET, TESCO_XAPI_KEY, MIKKU_API_KEY, JOB_SECRET.
