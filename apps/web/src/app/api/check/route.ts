// Server-side proxy to the backend's POST /check, so the browser needs no
// CORS setup and never learns the API address. The multipart body (including
// an uploaded picture or video) is streamed through unchanged.
// API_URL is read at request time: http://api:8000 inside Compose,
// http://localhost:8000 when run natively.

const API_URL = process.env.API_URL ?? "http://localhost:8000";

// Generous upper bound: reading media with a model can take a while.
const TIMEOUT_MS = 120_000;

export async function POST(request: Request): Promise<Response> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.startsWith("multipart/form-data")) {
    return Response.json({ detail: "Oczekiwano formularza (multipart/form-data)." }, { status: 415 });
  }

  try {
    const upstream = await fetch(`${API_URL}/check`, {
      method: "POST",
      headers: { "content-type": contentType },
      body: request.body,
      // Required by Node's fetch to send a streamed body.
      duplex: "half",
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    } as RequestInit & { duplex: "half" });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: "Nie można połączyć się z usługą sprawdzania." }, { status: 502 });
  }
}
