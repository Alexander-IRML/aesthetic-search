import { Container, getContainer } from "@cloudflare/containers";

const API_PORT = 8000;
const CONTAINER_NAME = "primary";
const LOGIN_ROUTE = "/alpha/login";
const LOGOUT_ROUTE = "/alpha/logout";
const MEDIA_ROUTE = "/media/";
const SESSION_COOKIE = "artsearch_alpha_session";
const SESSION_TTL_SECONDS = 12 * 60 * 60;
const textEncoder = new TextEncoder();

type RuntimeEnv = Env & {
  ALPHA_ACCESS_CODE: string;
  ALPHA_SESSION_KEY: string;
  DATABASE_URL: string;
  QDRANT_API_KEY: string;
  QDRANT_URL: string;
};

export class ArtSearchApiContainer extends Container {
  defaultPort = API_PORT;
  requiredPorts = [API_PORT];
  sleepAfter = "15m";
  enableInternet = true;
  pingEndpoint = "localhost:8000/health/ready";

  override onStart(): void {
    console.log("artsearch search container started");
  }

  override onStop(): void {
    console.log("artsearch search container stopped");
  }

  override onError(error: unknown): never {
    const errorName = error instanceof Error ? error.name : "UnknownError";
    console.error("artsearch search container error", errorName);
    throw error;
  }
}

export default {
  async fetch(request: Request, env: RuntimeEnv): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health/edge") {
      return Response.json({ status: "ok", layer: "cloudflare-worker" });
    }

    const missing = requiredSecretNames(env);
    if (missing.length > 0) {
      console.error("artsearch runtime secrets missing", missing.join(","));
      return unavailable("Search deployment is not configured.");
    }

    if (url.pathname === LOGIN_ROUTE) {
      return handleLogin(request, url, env);
    }
    if (url.pathname === LOGOUT_ROUTE) {
      return handleLogout(request, url);
    }
    if (!(await hasValidSession(request, env.ALPHA_SESSION_KEY))) {
      return requireAlphaAccess(request, url);
    }
    if (url.pathname.startsWith(MEDIA_ROUTE)) {
      return serveMedia(request, url, env);
    }

    const container = getContainer(env.ARTSEARCH_API_CONTAINER, CONTAINER_NAME);
    try {
      await container.startAndWaitForPorts({
        ports: API_PORT,
        startOptions: {
          envVars: {
            DATABASE_URL: env.DATABASE_URL,
            HF_HUB_OFFLINE: "1",
            MKL_NUM_THREADS: "1",
            OMP_NUM_THREADS: "1",
            QDRANT_API_KEY: env.QDRANT_API_KEY,
            QDRANT_URL: env.QDRANT_URL,
            TOKENIZERS_PARALLELISM: "false",
            TRANSFORMERS_OFFLINE: "1"
          }
        },
        cancellationOptions: {
          instanceGetTimeoutMS: 15_000,
          portReadyTimeoutMS: 180_000,
          waitInterval: 500
        }
      });
      return await container.fetch(request);
    } catch (error: unknown) {
      const errorName = error instanceof Error ? error.name : "UnknownError";
      console.error("artsearch search backend unavailable", errorName);
      return unavailable("The search model is warming up. Try again shortly.");
    }
  }
} satisfies ExportedHandler<RuntimeEnv>;

async function handleLogin(
  request: Request,
  url: URL,
  env: RuntimeEnv
): Promise<Response> {
  const fallbackNext = safeNextPath(url.searchParams.get("next"));
  if (request.method === "GET" || request.method === "HEAD") {
    if (await hasValidSession(request, env.ALPHA_SESSION_KEY)) {
      return Response.redirect(new URL(fallbackNext, url.origin).toString(), 302);
    }
    return loginPage(fallbackNext, false, request.method === "HEAD");
  }
  if (request.method !== "POST") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { Allow: "GET, HEAD, POST" }
    });
  }

  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return loginPage(fallbackNext, true, false, 400);
  }
  const providedCode = form.get("code");
  const nextPath = safeNextPath(form.get("next"));
  if (
    typeof providedCode !== "string" ||
    !(await equalSecret(providedCode, env.ALPHA_ACCESS_CODE))
  ) {
    return loginPage(nextPath, true, false, 401);
  }

  const expiresAt = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS;
  const token = await createSessionToken(expiresAt, env.ALPHA_SESSION_KEY);
  return new Response(null, {
    status: 303,
    headers: {
      "Cache-Control": "no-store",
      Location: nextPath,
      "Set-Cookie": sessionCookie(token, SESSION_TTL_SECONDS)
    }
  });
}

function handleLogout(request: Request, url: URL): Response {
  if (request.method !== "POST") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { Allow: "POST" }
    });
  }
  return new Response(null, {
    status: 303,
    headers: {
      "Cache-Control": "no-store",
      Location: new URL(LOGIN_ROUTE, url.origin).toString(),
      "Set-Cookie": sessionCookie("", 0)
    }
  });
}

function requireAlphaAccess(request: Request, url: URL): Response {
  const acceptsHtml = request.headers.get("Accept")?.includes("text/html") ?? false;
  if ((request.method === "GET" || request.method === "HEAD") && acceptsHtml) {
    const next = `${url.pathname}${url.search}`;
    const login = new URL(LOGIN_ROUTE, url.origin);
    login.searchParams.set("next", next);
    return Response.redirect(login.toString(), 302);
  }
  return Response.json(
    {
      error: {
        code: "alpha_access_required",
        message: "Alpha access is required."
      }
    },
    {
      status: 401,
      headers: {
        "Cache-Control": "no-store",
        "WWW-Authenticate": "ArtSearchAlpha"
      }
    }
  );
}

async function hasValidSession(request: Request, signingKey: string): Promise<boolean> {
  const token = cookieValue(request.headers.get("Cookie"), SESSION_COOKIE);
  if (token === null) {
    return false;
  }
  const parts = token.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") {
    return false;
  }
  const expiresAt = Number(parts[1]);
  const now = Math.floor(Date.now() / 1000);
  if (
    !Number.isSafeInteger(expiresAt) ||
    expiresAt <= now ||
    expiresAt > now + SESSION_TTL_SECONDS + 60
  ) {
    return false;
  }

  let signature: Uint8Array;
  try {
    signature = decodeBase64Url(parts[2]);
  } catch {
    return false;
  }
  const key = await importSigningKey(signingKey, ["verify"]);
  return crypto.subtle.verify(
    "HMAC",
    key,
    signature,
    textEncoder.encode(`v1.${expiresAt}`)
  );
}

async function createSessionToken(expiresAt: number, signingKey: string): Promise<string> {
  const payload = `v1.${expiresAt}`;
  const key = await importSigningKey(signingKey, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key, textEncoder.encode(payload));
  return `${payload}.${encodeBase64Url(signature)}`;
}

function importSigningKey(
  signingKey: string,
  usages: string[]
): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    textEncoder.encode(signingKey),
    { name: "HMAC", hash: "SHA-256" },
    false,
    usages
  );
}

async function equalSecret(provided: string, expected: string): Promise<boolean> {
  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", textEncoder.encode(provided)),
    crypto.subtle.digest("SHA-256", textEncoder.encode(expected))
  ]);
  const providedBytes = new Uint8Array(providedHash);
  const expectedBytes = new Uint8Array(expectedHash);
  let difference = providedBytes.length ^ expectedBytes.length;
  for (let index = 0; index < providedBytes.length; index += 1) {
    difference |= providedBytes[index] ^ (expectedBytes[index] ?? 0);
  }
  return difference === 0;
}

function cookieValue(header: string | null, name: string): string | null {
  if (header === null) {
    return null;
  }
  for (const item of header.split(";")) {
    const [candidate, ...valueParts] = item.trim().split("=");
    if (candidate === name) {
      return valueParts.join("=");
    }
  }
  return null;
}

function sessionCookie(value: string, maxAge: number): string {
  return [
    `${SESSION_COOKIE}=${value}`,
    "Path=/",
    `Max-Age=${maxAge}`,
    "HttpOnly",
    "Secure",
    "SameSite=Lax"
  ].join("; ");
}

function safeNextPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
    return "/search";
  }
  let parsed: URL;
  try {
    parsed = new URL(value, "https://alpha.invalid");
  } catch {
    return "/search";
  }
  if (
    parsed.origin !== "https://alpha.invalid" ||
    parsed.pathname === LOGIN_ROUTE
  ) {
    return "/search";
  }
  return `${parsed.pathname}${parsed.search}`;
}

function encodeBase64Url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function loginPage(
  nextPath: string,
  invalidCode: boolean,
  headOnly: boolean,
  status = 200
): Response {
  const error = invalidCode
    ? '<p class="error" role="alert">That access code did not match.</p>'
    : "";
  const body = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ArtSearch Closed Alpha</title>
  <style>
    :root { color-scheme: dark; font-family: Arial, Helvetica, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #090c0e; color: #e8f2ef; }
    main { width: min(92vw, 430px); border: 1px solid #3d5a57; border-radius: 6px; background: #111719; padding: 28px; box-shadow: 8px 8px 0 #052e2a; }
    .signal { color: #45dbc5; font: 700 12px/1.3 monospace; text-transform: uppercase; }
    h1 { margin: 10px 0 8px; font: 800 36px/1 monospace; letter-spacing: 0; }
    p { color: #b7c8c4; line-height: 1.5; }
    label { display: block; margin: 24px 0 8px; font: 700 13px/1.2 monospace; color: #d8e7e3; }
    input { width: 100%; min-height: 46px; border: 1px solid #55706c; border-radius: 4px; background: #070a0b; color: #fff; padding: 10px 12px; font: 16px/1.2 monospace; }
    input:focus { outline: 2px solid #45dbc5; outline-offset: 2px; }
    button { width: 100%; min-height: 46px; margin-top: 14px; border: 1px solid #45dbc5; border-radius: 4px; background: #45dbc5; color: #06110f; font: 800 14px/1 monospace; cursor: pointer; }
    button:hover { background: #75ead8; }
    .error { margin: 16px 0 0; color: #ff8d8d; font-weight: 700; }
    .status { margin-top: 24px; border-top: 1px solid #293735; padding-top: 14px; font: 12px/1.5 monospace; color: #78918c; }
  </style>
</head>
<body>
  <main>
    <div class="signal">Private signal // closed alpha</div>
    <h1>ARTSEARCH</h1>
    <p>Enter your alpha access code to continue.</p>
    ${error}
    <form method="post" action="${LOGIN_ROUTE}">
      <label for="code">ACCESS CODE</label>
      <input id="code" name="code" type="password" autocomplete="current-password" required autofocus>
      <input name="next" type="hidden" value="${escapeHtml(nextPath)}">
      <button type="submit">ENTER SEARCH</button>
    </form>
    <div class="status">SYSTEM: ONLINE<br>ACCESS: INVITE ONLY</div>
  </main>
</body>
</html>`;
  return new Response(headOnly ? null : body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
      "Content-Type": "text/html; charset=utf-8",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY"
    }
  });
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

async function serveMedia(request: Request, url: URL, env: RuntimeEnv): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { Allow: "GET, HEAD" }
    });
  }

  const key = mediaKey(url.pathname, env.ARTSEARCH_MEDIA_PREFIX);
  if (key === null) {
    return new Response("Not found", { status: 404 });
  }
  const object = await env.ARTSEARCH_MEDIA.get(key);
  if (object === null) {
    return new Response("Not found", { status: 404 });
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Cache-Control", "private, max-age=86400, immutable");
  headers.set("Cross-Origin-Resource-Policy", "same-origin");
  headers.set("ETag", object.httpEtag);
  headers.set("X-Content-Type-Options", "nosniff");
  const storedContentType = headers.get("Content-Type");
  if (storedContentType === null || storedContentType === "application/octet-stream") {
    headers.set("Content-Type", rasterContentType(key));
  }
  return new Response(request.method === "HEAD" ? null : object.body, { headers });
}

function rasterContentType(key: string): string {
  const extension = key.slice(key.lastIndexOf(".") + 1).toLowerCase();
  const types: Record<string, string> = {
    avif: "image/avif",
    gif: "image/gif",
    jpeg: "image/jpeg",
    jpg: "image/jpeg",
    png: "image/png",
    webp: "image/webp"
  };
  return types[extension] ?? "application/octet-stream";
}

function mediaKey(pathname: string, allowedPrefix: string): string | null {
  const encoded = pathname.slice(MEDIA_ROUTE.length);
  let key: string;
  try {
    key = decodeURIComponent(encoded);
  } catch {
    return null;
  }
  const parts = key.split("/");
  if (
    !key.startsWith(allowedPrefix) ||
    parts.some((part) => part.length === 0 || part === "." || part === "..")
  ) {
    return null;
  }
  return key;
}

function requiredSecretNames(env: RuntimeEnv): string[] {
  const values: Record<string, string | undefined> = {
    ALPHA_ACCESS_CODE: env.ALPHA_ACCESS_CODE,
    ALPHA_SESSION_KEY: env.ALPHA_SESSION_KEY,
    DATABASE_URL: env.DATABASE_URL,
    QDRANT_API_KEY: env.QDRANT_API_KEY,
    QDRANT_URL: env.QDRANT_URL
  };
  return Object.entries(values)
    .filter(([, value]) => !value)
    .map(([name]) => name);
}

function unavailable(message: string): Response {
  return Response.json(
    { error: { code: "backend_unavailable", message } },
    {
      status: 503,
      headers: {
        "Cache-Control": "no-store",
        "Retry-After": "15"
      }
    }
  );
}
