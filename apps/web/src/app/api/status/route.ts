// Server-side proxy to the backend readiness check, so the browser does not
// need CORS or to know the API address. API_URL is read at request time:
// http://api:8000 inside Compose, http://localhost:8000 when run natively.
//
// TODO: temporary. The check form calls this until the backend exposes a
// verification endpoint; then the form will call that instead.

const API_URL = process.env.API_URL ?? "http://localhost:8000";

export type StatusResponse =
  | { ok: true; dbRevision: string; checkedAt: string }
  | { ok: false; error: string; checkedAt: string };

export async function GET(): Promise<Response> {
  const checkedAt = new Date().toISOString();
  try {
    const response = await fetch(`${API_URL}/health/ready`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) {
      const error =
        response.status === 503
          ? "Baza danych jest niedostępna."
          : `Usługa odpowiedziała błędem (HTTP ${response.status}).`;
      return Response.json({ ok: false, error, checkedAt } satisfies StatusResponse, { status: 502 });
    }
    const body = (await response.json()) as { db_revision?: string };
    return Response.json({
      ok: true,
      dbRevision: body.db_revision ?? "nieznana",
      checkedAt,
    } satisfies StatusResponse);
  } catch {
    return Response.json(
      { ok: false, error: "Nie można połączyć się z usługą.", checkedAt } satisfies StatusResponse,
      { status: 502 },
    );
  }
}
