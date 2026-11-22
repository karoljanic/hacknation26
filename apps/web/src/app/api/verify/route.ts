// Server-side proxy to the backend's POST /verify.
// Streams multipart/form-data (text or screenshot + optional hash) unchanged.

const API_URL = process.env.API_URL ?? "http://localhost:8000";
const TIMEOUT_MS = 60_000;

export async function POST(request: Request): Promise<Response> {
    const contentType = request.headers.get("content-type") ?? "";
    if (!contentType.startsWith("multipart/form-data")) {
        return Response.json({ detail: "Oczekiwano formularza (multipart/form-data)." }, { status: 415 });
    }

    try {
        const headers: Record<string, string> = { "content-type": contentType };
        const forwardedFor = request.headers.get("x-forwarded-for");
        if (forwardedFor) headers["x-forwarded-for"] = forwardedFor;

        const upstream = await fetch(`${API_URL}/verify`, {
            method: "POST",
            headers,
            body: request.body,
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