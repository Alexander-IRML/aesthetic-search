import { Container, getContainer } from "@cloudflare/containers";
import { handleRetrievalAssistant } from "./assistant";

const API_PORT = 8000;
const CONTAINER_NAME = "primary";
const MAX_EDGE_UPLOAD_BYTES = 11_000_000;
const MEDIA_ROUTE = "/media/";
const ASSISTANT_ROUTE = "/api/retrieval-assistant";
const RUNTIME_STATUS_ROUTE = "/api/runtime-status";
const FRONTEND_ASSETS = new Map<string, string>([
  ["/search", "/index.html"],
  ["/assets/app.css", "/app.css"],
  ["/assets/app.js", "/app.js"],
]);
const CONTAINER_ROUTES = new Set([
  "/api/docs",
  "/api/openapi.json",
  "/api/result-feedback",
  "/api/search-by-image",
  "/health/live",
  "/health/ready",
]);

type RuntimeEnv = Env & {
  AI: Ai;
  ASSETS: Fetcher;
  ASSISTANT_LOCATION_RATE_LIMITER: RateLimit;
  ASSISTANT_RATE_LIMITER: RateLimit;
  DATABASE_URL: string;
  FEEDBACK_RATE_LIMITER: RateLimit;
  QDRANT_API_KEY: string;
  QDRANT_URL: string;
  SEARCH_LOCATION_RATE_LIMITER: RateLimit;
  SEARCH_RATE_LIMITER: RateLimit;
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
      return Response.json(
        { status: "ok", layer: "cloudflare-worker", access: "public" },
        { headers: { "Cache-Control": "no-store" } },
      );
    }
    if (url.pathname === RUNTIME_STATUS_ROUTE) {
      return serveRuntimeStatus(request, env);
    }
    if (url.pathname === "/" || url.pathname.startsWith("/alpha/")) {
      return Response.redirect(new URL("/search", url.origin).toString(), 302);
    }

    const assetPath = FRONTEND_ASSETS.get(url.pathname);
    if (assetPath !== undefined) {
      return serveFrontendAsset(request, url, assetPath, env);
    }
    if (url.pathname.startsWith(MEDIA_ROUTE)) {
      return serveMedia(request, url, env);
    }
    if (url.pathname === ASSISTANT_ROUTE) {
      return handleRetrievalAssistant(request, env);
    }
    if (!CONTAINER_ROUTES.has(url.pathname)) {
      return new Response("Not found", {
        status: 404,
        headers: { "Cache-Control": "no-store" },
      });
    }

    const missing = requiredSecretNames(env);
    if (missing.length > 0) {
      console.error("artsearch runtime secrets missing", missing.join(","));
      return unavailable("Search deployment is not configured.");
    }
    if (request.method === "POST" && url.pathname === "/api/search-by-image") {
      const allowed = await allowSearch(request, env);
      if (!allowed) {
        return rateLimited("Too many image searches. Try again in a minute.");
      }
      const invalidRequest = validateSearchRequest(request);
      if (invalidRequest !== null) {
        return invalidRequest;
      }
    }
    if (request.method === "POST" && url.pathname === "/api/result-feedback") {
      const allowed = await allowFeedback(request, env);
      if (!allowed) {
        return rateLimited("Too many feedback events. Try again in a minute.");
      }
    }

    const edgeStarted = performance.now();
    const container = getContainer(env.ARTSEARCH_API_CONTAINER, CONTAINER_NAME);
    let initialState = "unknown";
    try {
      initialState = (await container.getState()).status;
    } catch (error: unknown) {
      const errorName = error instanceof Error ? error.name : "UnknownError";
      console.warn("artsearch container state unavailable", errorName);
    }
    const waitStarted = performance.now();
    try {
      await container.startAndWaitForPorts({
        ports: API_PORT,
        startOptions: {
          envVars: {
            DATABASE_URL: env.DATABASE_URL,
            HF_HUB_OFFLINE: "1",
            MKL_NUM_THREADS: "2",
            OMP_NUM_THREADS: "2",
            QDRANT_API_KEY: env.QDRANT_API_KEY,
            QDRANT_URL: env.QDRANT_URL,
            TOKENIZERS_PARALLELISM: "false",
            TRANSFORMERS_OFFLINE: "1",
          },
        },
        cancellationOptions: {
          instanceGetTimeoutMS: 15_000,
          portReadyTimeoutMS: 180_000,
          waitInterval: 500,
        },
      });
      const containerWaitMs = elapsedMs(waitStarted);
      const fetchStarted = performance.now();
      const response = await container.fetch(request);
      const containerFetchMs = elapsedMs(fetchStarted);
      const edgeTotalMs = elapsedMs(edgeStarted);
      console.log(JSON.stringify({
        event: "artsearch.edge_request_completed",
        path: url.pathname,
        initial_container_state: initialState,
        container_wait_ms: roundedMs(containerWaitMs),
        container_fetch_ms: roundedMs(containerFetchMs),
        edge_total_ms: roundedMs(edgeTotalMs),
        status: response.status,
      }));
      return withEdgeTiming(
        response,
        initialState,
        containerWaitMs,
        containerFetchMs,
        edgeTotalMs,
      );
    } catch (error: unknown) {
      const errorName = error instanceof Error ? error.name : "UnknownError";
      console.error("artsearch search backend unavailable", errorName);
      return unavailable(
        "The search model is warming up. Try again shortly.",
        edgeTimingHeaders(
          initialState,
          elapsedMs(waitStarted),
          0,
          elapsedMs(edgeStarted),
        ),
      );
    }
  },
} satisfies ExportedHandler<RuntimeEnv>;

async function serveRuntimeStatus(request: Request, env: RuntimeEnv): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { Allow: "GET, HEAD", "Cache-Control": "no-store" },
    });
  }

  const container = getContainer(env.ARTSEARCH_API_CONTAINER, CONTAINER_NAME);
  let payload: Record<string, boolean | number | string>;
  try {
    const state = await container.getState();
    payload = {
      status: state.status === "healthy"
        ? "ready"
        : (state.status === "running" ? "warming" : "cold"),
      warm: state.status === "healthy",
      container_state: state.status,
      observed_at: new Date().toISOString(),
      last_change_at: new Date(state.lastChange).toISOString(),
    };
  } catch (error: unknown) {
    const errorName = error instanceof Error ? error.name : "UnknownError";
    console.warn("artsearch runtime status unavailable", errorName);
    payload = {
      status: "unknown",
      warm: false,
      container_state: "unknown",
      observed_at: new Date().toISOString(),
    };
  }

  const response = Response.json(payload, {
    headers: { "Cache-Control": "no-store" },
  });
  return request.method === "HEAD"
    ? new Response(null, { status: response.status, headers: response.headers })
    : response;
}

function withEdgeTiming(
  response: Response,
  initialState: string,
  containerWaitMs: number,
  containerFetchMs: number,
  edgeTotalMs: number,
): Response {
  const headers = new Headers(response.headers);
  const timing = edgeTimingHeaders(
    initialState,
    containerWaitMs,
    containerFetchMs,
    edgeTotalMs,
  );
  for (const [name, value] of timing) {
    if (name === "Server-Timing" && headers.has(name)) {
      headers.set(name, `${headers.get(name)}, ${value}`);
    } else {
      headers.set(name, value);
    }
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function edgeTimingHeaders(
  initialState: string,
  containerWaitMs: number,
  containerFetchMs: number,
  edgeTotalMs: number,
): Headers {
  const headers = new Headers();
  headers.set("X-ArtSearch-Container-State", initialState);
  headers.set("X-ArtSearch-Container-Wait-Ms", roundedMs(containerWaitMs).toString());
  headers.set("X-ArtSearch-Container-Fetch-Ms", roundedMs(containerFetchMs).toString());
  headers.set("X-ArtSearch-Edge-Total-Ms", roundedMs(edgeTotalMs).toString());
  headers.set(
    "Server-Timing",
    [
      `container_wait;dur=${roundedMs(containerWaitMs)}`,
      `container_fetch;dur=${roundedMs(containerFetchMs)}`,
      `edge_total;dur=${roundedMs(edgeTotalMs)}`,
    ].join(", "),
  );
  return headers;
}

function elapsedMs(started: number): number {
  return Math.max(0, performance.now() - started);
}

function roundedMs(value: number): number {
  return Math.round(value * 10) / 10;
}

async function serveFrontendAsset(
  request: Request,
  url: URL,
  assetPath: string,
  env: RuntimeEnv,
): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { Allow: "GET, HEAD" },
    });
  }

  const assetUrl = new URL(assetPath, url.origin);
  const assetRequest = new Request(assetUrl, {
    method: request.method,
    headers: request.headers,
  });
  const asset = await env.ASSETS.fetch(assetRequest);
  const headers = new Headers(asset.headers);
  headers.set(
    "Cache-Control",
    assetPath === "/index.html" ? "public, max-age=60" : "public, max-age=3600",
  );
  headers.set(
    "Content-Security-Policy",
    "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; " +
      "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
  );
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  return new Response(request.method === "HEAD" ? null : asset.body, {
    status: asset.status,
    statusText: asset.statusText,
    headers,
  });
}

async function allowSearch(request: Request, env: RuntimeEnv): Promise<boolean> {
  const clientKey = request.headers.get("CF-Connecting-IP") ?? "unknown-client";
  const [client, location] = await Promise.all([
    env.SEARCH_RATE_LIMITER.limit({ key: clientKey }),
    env.SEARCH_LOCATION_RATE_LIMITER.limit({ key: "public-search" }),
  ]);
  return client.success && location.success;
}

function validateSearchRequest(request: Request): Response | null {
  const contentType = request.headers.get("Content-Type") ?? "";
  if (!contentType.toLowerCase().startsWith("multipart/form-data")) {
    return Response.json(
      {
        error: {
          code: "invalid_content_type",
          message: "Image search requires a multipart image upload.",
        },
      },
      { status: 415, headers: { "Cache-Control": "no-store" } },
    );
  }
  const contentLength = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(contentLength) && contentLength > MAX_EDGE_UPLOAD_BYTES) {
    return Response.json(
      {
        error: {
          code: "upload_too_large",
          message: "Choose an image smaller than 10 MB.",
        },
      },
      { status: 413, headers: { "Cache-Control": "no-store" } },
    );
  }
  return null;
}

async function allowFeedback(request: Request, env: RuntimeEnv): Promise<boolean> {
  const clientKey = request.headers.get("CF-Connecting-IP") ?? "unknown-client";
  const result = await env.FEEDBACK_RATE_LIMITER.limit({ key: clientKey });
  return result.success;
}

function rateLimited(message: string): Response {
  return Response.json(
    { error: { code: "rate_limited", message } },
    {
      status: 429,
      headers: {
        "Cache-Control": "no-store",
        "Retry-After": "60",
      },
    },
  );
}

async function serveMedia(request: Request, url: URL, env: RuntimeEnv): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { Allow: "GET, HEAD" },
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
  headers.set("Cache-Control", "public, max-age=86400, immutable");
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
    webp: "image/webp",
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
    DATABASE_URL: env.DATABASE_URL,
    QDRANT_API_KEY: env.QDRANT_API_KEY,
    QDRANT_URL: env.QDRANT_URL,
  };
  return Object.entries(values)
    .filter(([, value]) => !value)
    .map(([name]) => name);
}

function unavailable(message: string, extraHeaders: Headers = new Headers()): Response {
  const headers = new Headers(extraHeaders);
  headers.set("Cache-Control", "no-store");
  headers.set("Retry-After", "15");
  return Response.json(
    { error: { code: "backend_unavailable", message } },
    {
      status: 503,
      headers,
    },
  );
}
