// Server-side proxy to the backend's /admin/* endpoints.
// Keeps API_URL and optional PRAWDOWID_ADMIN_TOKEN off the client bundle.

const API_URL = process.env.API_URL ?? "http://localhost:8000";
const SERVER_ADMIN_TOKEN = process.env.PRAWDOWID_ADMIN_TOKEN ?? "";
const TIMEOUT_MS = 30_000;
const SAFE_SEGMENT = /^[a-zA-Z0-9._-]+$/;

type RouteContext = {
    params: Promise<{ path: string[] }>;
};

async function forward(request: Request, context: RouteContext): Promise<Response> {
    const { path } = await context.params;
    if (!path.length || path.some((segment) => segment === ".." || !SAFE_SEGMENT.test(segment))) {
        return Response.json({ detail: "Nieprawidłowa ścieżka żądania." }, { status: 400 });
    }

    const incomingUrl = new URL(request.url);
    const targetUrl = `${API_URL}/admin/${path.join("/")}${incomingUrl.search}`;

    const headers: Record<string, string> = {};
    const contentType = request.headers.get("content-type");
    if (contentType) headers["content-type"] = contentType;

    const clientToken = request.headers.get("x-admin-token");
    const token = clientToken || SERVER_ADMIN_TOKEN;
    if (token) headers["x-admin-token"] = token;

    try {
        const hasBody = request.method !== "GET" && request.method !== "HEAD";
        const upstream = await fetch(targetUrl, {
            method: request.method,
            headers,
            body: hasBody ? await request.text() : undefined,
            cache: "no-store",
            signal: AbortSignal.timeout(TIMEOUT_MS),
        });

        const responseHeaders: Record<string, string> = {
            "content-type": upstream.headers.get("content-type") ?? "application/json",
        };
        const disposition = upstream.headers.get("content-disposition");
        if (disposition) responseHeaders["content-disposition"] = disposition;

        return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
    } catch {
        return Response.json({ detail: "Nie można połączyć się z usługą panelu administratora." }, { status: 502 });
    }
}

export const GET = forward;
export const POST = forward;
export const DELETE = forward;