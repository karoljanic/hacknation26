// Server-side proxy to the backend's POST /verify.
// Streams multipart/form-data (image + optional hash) unchanged.
// API_URL is read at request time: http://api:8000 inside Compose,
// http://localhost:8000 when run natively.

const API_URL = process.env.API_URL ?? "http://localhost:8000";

// Verification is cheaper than a full check, but image parsing still takes time.
const TIMEOUT_MS = 60_000;

export async function POST(request: Request): Promise<Response> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.startsWith("multipart/form-data")) {
    return Response.json({ detail: "Oczekiwano formularza (multipart/form-data)." }, { status: 415 });
  }

  try {
    // Rate limiting is per client: pass on the address a reverse proxy in
    // front of this app recorded (the API honours it only when configured to).
    const headers: Record<string, string> = { "content-type": contentType };
    const forwardedFor = request.headers.get("x-forwarded-for");
    if (forwardedFor) headers["x-forwarded-for"] = forwardedFor;

    const upstream = await fetch(`${API_URL}/verify`, {
      method: "POST",
      headers,
      body: request.body,
      // Required by Node's fetch to send a streamed body.
      duplex: "half",
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    } as RequestInit & { duplex: "half" });

    const responseHeaders: Record<string, string> = {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    };
    const retryAfter = upstream.headers.get("retry-after");
    if (retryAfter) responseHeaders["retry-after"] = retryAfter;

    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch {
    return Response.json({ detail: "Nie można połączyć się z usługą weryfikacji podpisu." }, { status: 502 });
  }
}
