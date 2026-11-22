"use client";

import { useCallback, useEffect, useState } from "react";

type Overview = {
    auth_enabled: boolean;
    window_days: number | null;
    total_runs: number;
    runs_by_status: Record<string, number>;
    tech_failure_rate: number;
    total_assessments: number;
    verdicts: Record<string, number>;
    abstention_rate: number;
    channels: Record<string, number>;
    latency_median_ms: number | null;
    latency_p95_ms: number | null;
    latency_avg_ms: number | null;
    injection_incidents: number;
    expired_submissions: number;
};

type HotTopic = {
    claim_id: string;
    text: string;
    kind: string;
    submission_count: number;
    channels: string[];
    first_seen_at: string | null;
    last_seen_at: string | null;
    latest_verdict: string | null;
    latest_strength: string | null;
    latest_assessed_at: string | null;
    needs_attention: boolean;
};

type HotTopicsPage = {
    items: HotTopic[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
    min_submissions: number;
};

type RecentRun = {
    run_id: string;
    submission_id: string;
    channel: string;
    input_preview: string;
    url: string | null;
    status: string;
    started_at: string | null;
    finished_at: string | null;
    latency_ms: number | null;
    verdicts: string[];
    injection_count: number;
    error: string | null;
    expires_at: string | null;
};

type InjectionAlert = {
    run_id: string;
    submission_id: string;
    channel: string;
    detected_at: string | null;
    where: string | null;
    claim_id: string | null;
    excerpt: string;
    detected_by: string;
};

type Tab = "topics" | "quality" | "security";

const WINDOW_OPTIONS: { label: string; value: string }[] = [
    { label: "Ostatnie 24 h", value: "1" },
    { label: "7 dni", value: "7" },
    { label: "30 dni", value: "30" },
    { label: "Cały okres", value: "all" },
];

const MIN_SUBMISSIONS_OPTIONS = [2, 3, 5, 10, 20];
const PAGE_SIZE_OPTIONS = [5, 10, 20, 50];

const VERDICT_LABEL: Record<string, string> = {
    confirmed: "Potwierdzone",
    false: "Fałszywe",
    misleading: "Wprowadzające w błąd",
    insufficient_evidence: "Brak wystarczających dowodów",
    unverifiable: "Nie podlega weryfikacji",
};

const STATUS_LABEL: Record<string, string> = {
    ok: "OK",
    tech_failure: "Awaria techniczna",
    needs_clarification: "Wymaga doprecyzowania",
    out_of_scope: "Poza zakresem",
    running: "W trakcie",
    pending: "Oczekuje",
};

const DETECTOR_LABEL: Record<string, string> = {
    input_heuristic: "Filtr wejścia",
    hidden_html_heuristic: "Ukryty tekst HTML",
    evidence_heuristic: "Filtr treści źródeł",
    extraction_model: "Model ekstrakcji",
    assessment_model: "Model oceniający",
    heuristic: "Filtr heurystyczny",
};

// "guard:jev,guardrails": the request was rejected before any check.
function detectorLabel(detectedBy: string): string {
    if (detectedBy.startsWith("guard:")) {
        return `Zablokowane na wejściu (${detectedBy.slice("guard:".length).replace(",", ", ")})`;
    }
    return DETECTOR_LABEL[detectedBy] ?? detectedBy;
}

const EMPTY_TOPICS_PAGE: HotTopicsPage = {
    items: [],
    total: 0,
    page: 1,
    page_size: 10,
    total_pages: 1,
    min_submissions: 2,
};

function fmtDate(iso: string | null): string {
    if (!iso) return "–";
    return new Date(iso).toLocaleString("pl-PL", {
        dateStyle: "short",
        timeStyle: "short",
    });
}

function fmtSeconds(ms: number | null): string {
    if (ms === null) return "–";
    return `${(ms / 1000).toLocaleString("pl-PL", { maximumFractionDigits: 1 })} s`;
}

function fmtPct(ratio: number): string {
    return `${Math.round(ratio * 100)}%`;
}

export function AdminDashboard() {
    const [tab, setTab] = useState<Tab>("topics");
    const [windowVal, setWindowVal] = useState<string>("7");
    const [onlyUnresolved, setOnlyUnresolved] = useState(false);
    const [minSubmissions, setMinSubmissions] = useState<number>(2);
    const [topicsPageNum, setTopicsPageNum] = useState<number>(1);
    const [topicsPageSize, setTopicsPageSize] = useState<number>(10);

    const [loggedIn, setLoggedIn] = useState(false);
    const [needsAuth, setNeedsAuth] = useState(false);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    const [overview, setOverview] = useState<Overview | null>(null);
    const [topicsPage, setTopicsPage] = useState<HotTopicsPage>(EMPTY_TOPICS_PAGE);
    const [runs, setRuns] = useState<RecentRun[]>([]);
    const [injections, setInjections] = useState<InjectionAlert[]>([]);
    const [busyId, setBusyId] = useState<string | null>(null);

    const apiFetch = useCallback(
        async (path: string, init?: RequestInit) => {
            return fetch(`/api/admin/${path}`, { ...init, cache: "no-store" });
        },
        [],
    );

    const loadAll = useCallback(async () => {
        setLoading(true);
        setError(null);
        const daysParam = windowVal === "all" ? "" : `days=${windowVal}`;
        const q = (extra: string[] = []) => {
            const parts = [daysParam, ...extra].filter(Boolean);
            return parts.length ? `?${parts.join("&")}` : "";
        };

        const topicParams = [
            `min_submissions=${minSubmissions}`,
            `page=${topicsPageNum}`,
            `page_size=${topicsPageSize}`,
            ...(onlyUnresolved ? ["only_unresolved=true"] : []),
        ];

        try {
            const [ovRes, topRes, runRes, injRes] = await Promise.all([
                apiFetch(`overview${q()}`),
                apiFetch(`hot-topics${q(topicParams)}`),
                apiFetch(`runs${q(["limit=40"])}`),
                apiFetch(`injections${q(["limit=50"])}`),
            ]);

            const responses = [ovRes, topRes, runRes, injRes];
            if (responses.some((r) => r.status === 401)) {
                setNeedsAuth(true);
                setLoading(false);
                return;
            }

            const failed = responses.find((r) => !r.ok);
            if (failed) {
                const body = (await failed.json().catch(() => ({}))) as { detail?: string };
                throw new Error(body.detail ?? `Błąd pobierania danych (HTTP ${failed.status}).`);
            }

            setNeedsAuth(false);
            setOverview((await ovRes.json()) as Overview);

            const parsedTopics = (await topRes.json()) as HotTopicsPage;
            setTopicsPage(parsedTopics);
            if (parsedTopics.page !== topicsPageNum) {
                setTopicsPageNum(parsedTopics.page);
            }

            setRuns((await runRes.json()) as RecentRun[]);
            setInjections((await injRes.json()) as InjectionAlert[]);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Nie udało się załadować danych panelu.");
        } finally {
            setLoading(false);
        }
    }, [apiFetch, windowVal, onlyUnresolved, minSubmissions, topicsPageNum, topicsPageSize]);

    useEffect(() => {
        if (loggedIn) void loadAll();
    }, [loadAll, loggedIn]);

    async function handleDeleteSubmission(submissionId: string) {
        setBusyId(submissionId);
        setNotice(null);
        setError(null);
        try {
            const res = await apiFetch(`submissions/${submissionId}`, { method: "DELETE" });
            if (!res.ok) {
                const body = (await res.json().catch(() => ({}))) as { detail?: string };
                throw new Error(body.detail ?? "Nie udało się usunąć sesji.");
            }
            setNotice(`Usunięto zgłoszenie/sesję ${submissionId.slice(0, 8)}….`);
            await loadAll();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Błąd podczas usuwania sesji.");
        } finally {
            setBusyId(null);
        }
    }

    async function handlePurgeExpired() {
        setBusyId("purge");
        setNotice(null);
        setError(null);
        try {
            const res = await apiFetch("retention/purge", { method: "POST" });
            if (!res.ok) throw new Error("Nie udało się wyczyścić wygasłych zgłoszeń.");
            const data = (await res.json()) as { deleted_submissions: number };
            setNotice(`Usunięto wygasłych zgłoszeń: ${data.deleted_submissions}.`);
            await loadAll();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Błąd czyszczenia retencji.");
        } finally {
            setBusyId(null);
        }
    }

    async function handleDownload(endpoint: string, filename: string) {
        setError(null);
        const daysParam = windowVal === "all" ? "" : `?days=${windowVal}`;
        try {
            const res = await apiFetch(`${endpoint}${daysParam}`);
            if (!res.ok) throw new Error("Nie udało się wygenerować eksportu.");
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement("a");
            anchor.href = url;
            anchor.download = filename;
            anchor.click();
            URL.revokeObjectURL(url);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Błąd eksportu danych.");
        }
    }

    if (!loggedIn) {
        return (
            <button type="button" className="button" onClick={() => setLoggedIn(true)}>
                Zaloguj do panelu
            </button>
        );
    }

    if (needsAuth) {
        return (
            <div className="alert alert--error">
                <p>Panel administratora nie jest skonfigurowany na serwerze.</p>
            </div>
        );
    }

    return (
        <div className="admin">
            <div className="admin-toolbar">
                <div className="admin-toolbar__group">
                    <label htmlFor="admin-window" className="field__label">
                        Zakres czasu:
                    </label>
                    <select
                        id="admin-window"
                        className="input admin-select"
                        value={windowVal}
                        onChange={(e) => {
                            setWindowVal(e.target.value);
                            setTopicsPageNum(1);
                        }}
                    >
                        {WINDOW_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                                {opt.label}
                            </option>
                        ))}
                    </select>
                </div>

                <div className="admin-toolbar__actions">
                    <button
                        type="button"
                        className="button button--secondary"
                        onClick={() => void handleDownload("export/runs.csv", "prawdowid-przebiegi.csv")}
                    >
                        Eksport CSV
                    </button>
                    <button
                        type="button"
                        className="button button--secondary"
                        onClick={() => void handleDownload("export/analytics.json", "prawdowid-analiza-fimi.json")}
                    >
                        Eksport JSON
                    </button>
                    <button type="button" className="button" onClick={() => void loadAll()} disabled={loading}>
                        {loading ? "Odświeżam…" : "Odśwież"}
                    </button>
                </div>
            </div>

            {error && (
                <div className="alert alert--error" role="alert">
                    <p className="alert__title">Błąd</p>
                    <p>{error}</p>
                </div>
            )}
            {notice && (
                <div className="alert alert--success" role="status">
                    <p>{notice}</p>
                </div>
            )}

            {overview && (
                <section aria-label="Kluczowe wskaźniki" className="kpi-grid">
                    <div className="kpi-card">
                        <span className="kpi-card__label">Sprawdzenia</span>
                        <strong className="kpi-card__value">{overview.total_runs}</strong>
                        <span className="kpi-card__hint">Awarie techniczne: {fmtPct(overview.tech_failure_rate)}</span>
                    </div>
                    <div className="kpi-card">
                        <span className="kpi-card__label">Mediana czasu</span>
                        <strong className="kpi-card__value">{fmtSeconds(overview.latency_median_ms)}</strong>
                        <span className="kpi-card__hint">p95: {fmtSeconds(overview.latency_p95_ms)}</span>
                    </div>
                    <div className="kpi-card">
                        <span className="kpi-card__label">Wstrzymania (brak dowodów)</span>
                        <strong className="kpi-card__value">{fmtPct(overview.abstention_rate)}</strong>
                        <span className="kpi-card__hint">Z {overview.total_assessments} ocen twierdzeń</span>
                    </div>
                    <div className="kpi-card">
                        <span className="kpi-card__label">Próby prompt injection</span>
                        <strong className="kpi-card__value">{overview.injection_incidents}</strong>
                        <span className="kpi-card__hint">Zignorowane i odnotowane</span>
                    </div>
                </section>
            )}

            <nav className="admin-tabs" aria-label="Sekcje panelu administratora">
                <button
                    type="button"
                    className={`admin-tab ${tab === "topics" ? "admin-tab--active" : ""}`}
                    onClick={() => setTab("topics")}
                >
                    Hot topics ({topicsPage.total})
                </button>
                <button
                    type="button"
                    className={`admin-tab ${tab === "quality" ? "admin-tab--active" : ""}`}
                    onClick={() => setTab("quality")}
                >
                    Jakość i przebiegi ({overview?.total_runs ?? runs.length})
                </button>
                <button
                    type="button"
                    className={`admin-tab ${tab === "security" ? "admin-tab--active" : ""}`}
                    onClick={() => setTab("security")}
                >
                    Prompt injection ({injections.length})
                </button>
            </nav>

            {tab === "topics" && (
                <section className="admin-section">
                    <div className="admin-section__header">
                        <h2>Hot topics — najczęściej wyszukiwane tematy</h2>
                        <div className="admin-filters">
                            <label htmlFor="admin-min-submissions" className="admin-inline-field">
                                <span>Min. zapytań:</span>
                                <select
                                    id="admin-min-submissions"
                                    className="input admin-select--compact"
                                    value={minSubmissions}
                                    onChange={(e) => {
                                        setMinSubmissions(Number(e.target.value));
                                        setTopicsPageNum(1);
                                    }}
                                >
                                    {MIN_SUBMISSIONS_OPTIONS.map((count) => (
                                        <option key={count} value={count}>
                                            ≥ {count}
                                        </option>
                                    ))}
                                </select>
                            </label>

                            <label className="admin-checkbox">
                                <input
                                    type="checkbox"
                                    checked={onlyUnresolved}
                                    onChange={(e) => {
                                        setOnlyUnresolved(e.target.checked);
                                        setTopicsPageNum(1);
                                    }}
                                />
                                Tylko nierozstrzygnięte (brak dowodów)
                            </label>
                        </div>
                    </div>

                    {topicsPage.items.length === 0 ? (
                        <p className="field__hint">
                            Brak hot topics z co najmniej {minSubmissions} zapytaniami w wybranym okresie.
                        </p>
                    ) : (
                        <>
                            <div className="table-wrap">
                                <table className="admin-table">
                                    <thead>
                                        <tr>
                                            <th>Zgłoszenia</th>
                                            <th>Twierdzenie / temat</th>
                                            <th>Kanały</th>
                                            <th>Ostatni werdykt</th>
                                            <th>Ostatnio widziane</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {topicsPage.items.map((topic) => (
                                            <tr key={topic.claim_id} className={topic.needs_attention ? "admin-row--alert" : undefined}>
                                                <td>
                                                    <strong>{topic.submission_count}</strong>
                                                </td>
                                                <td>
                                                    <div>{topic.text}</div>
                                                    {topic.needs_attention && (
                                                        <span className="admin-pill admin-pill--warn">Powtarzający się brak źródeł</span>
                                                    )}
                                                </td>
                                                <td>{topic.channels.join(", ") || "–"}</td>
                                                <td>
                                                    {topic.latest_verdict ? VERDICT_LABEL[topic.latest_verdict] ?? topic.latest_verdict : "–"}
                                                </td>
                                                <td>{fmtDate(topic.last_seen_at)}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>

                            <div className="admin-pagination">
                                <span className="admin-pagination__info">
                                    Strona {topicsPage.page} z {topicsPage.total_pages} (łącznie: {topicsPage.total})
                                </span>
                                <div className="admin-pagination__controls">
                                    <label htmlFor="admin-page-size" className="admin-inline-field">
                                        <span>Na stronę:</span>
                                        <select
                                            id="admin-page-size"
                                            className="input admin-select--compact"
                                            value={topicsPageSize}
                                            onChange={(e) => {
                                                setTopicsPageSize(Number(e.target.value));
                                                setTopicsPageNum(1);
                                            }}
                                        >
                                            {PAGE_SIZE_OPTIONS.map((size) => (
                                                <option key={size} value={size}>
                                                    {size}
                                                </option>
                                            ))}
                                        </select>
                                    </label>
                                    <button
                                        type="button"
                                        className="button button--secondary"
                                        disabled={topicsPage.page <= 1 || loading}
                                        onClick={() => setTopicsPageNum((p) => Math.max(1, p - 1))}
                                    >
                                        Poprzednia
                                    </button>
                                    <button
                                        type="button"
                                        className="button button--secondary"
                                        disabled={topicsPage.page >= topicsPage.total_pages || loading}
                                        onClick={() => setTopicsPageNum((p) => Math.min(topicsPage.total_pages, p + 1))}
                                    >
                                        Następna
                                    </button>
                                </div>
                            </div>
                        </>
                    )}
                </section>
            )}

            {tab === "quality" && (
                <section className="admin-section">
                    <h2>Rozkład werdyktów i kanałów</h2>
                    {overview && (
                        <div className="admin-split">
                            <div className="admin-box">
                                <h3>Werdykty</h3>
                                <ul className="admin-kv">
                                    {Object.entries(VERDICT_LABEL).map(([key, label]) => (
                                        <li key={key}>
                                            <span>{label}</span>
                                            <strong>{overview.verdicts[key] ?? 0}</strong>
                                        </li>
                                    ))}
                                </ul>
                            </div>
                            <div className="admin-box">
                                <h3>Kanały zgłoszeń</h3>
                                <ul className="admin-kv">
                                    {Object.keys(overview.channels).length === 0 ? (
                                        <li>Brak danych</li>
                                    ) : (
                                        Object.entries(overview.channels).map(([channel, count]) => (
                                            <li key={channel}>
                                                <span>{channel}</span>
                                                <strong>{count}</strong>
                                            </li>
                                        ))
                                    )}
                                </ul>
                            </div>
                        </div>
                    )}

                    <div className="admin-section__header">
                        <h2>Dziennik ostatnich sprawdzeń i zarządzanie retencją</h2>
                        <button
                            type="button"
                            className="button button--secondary"
                            disabled={busyId === "purge" || (overview?.expired_submissions ?? 0) === 0}
                            onClick={() => void handlePurgeExpired()}
                        >
                            Wyczyść wygasłe ({overview?.expired_submissions ?? 0})
                        </button>
                    </div>

                    <div className="table-wrap">
                        <table className="admin-table">
                            <thead>
                                <tr>
                                    <th>Czas</th>
                                    <th>Kanał</th>
                                    <th>Status</th>
                                    <th>Treść / URL (po maskowaniu)</th>
                                    <th>Werdykty</th>
                                    <th>Czas odp.</th>
                                    <th>Sesja (RODO/demo)</th>
                                </tr>
                            </thead>
                            <tbody>
                                {runs.map((run) => (
                                    <tr key={run.run_id}>
                                        <td>{fmtDate(run.started_at)}</td>
                                        <td>{run.channel}</td>
                                        <td>{STATUS_LABEL[run.status] ?? run.status}</td>
                                        <td className="admin-cell--wide">
                                            {run.input_preview}
                                            {run.error && <div className="field__error">{run.error}</div>}
                                        </td>
                                        <td>
                                            {run.verdicts.length
                                                ? run.verdicts.map((v) => VERDICT_LABEL[v] ?? v).join(", ")
                                                : "–"}
                                        </td>
                                        <td>{fmtSeconds(run.latency_ms)}</td>
                                        <td>
                                            <button
                                                type="button"
                                                className="link-button"
                                                disabled={busyId === run.submission_id}
                                                onClick={() => void handleDeleteSubmission(run.submission_id)}
                                            >
                                                Usuń sesję
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </section>
            )}

            {tab === "security" && (
                <section className="admin-section">
                    <h2>Wykryte próby prompt injection</h2>
                    <p className="field__hint">
                        Każda próba manipulacji wykryta w zgłoszeniu lub analizowanych materiałach źródłowych jest ignorowana
                        podczas wydawania werdyktu i rejestrowana tutaj do przeglądu bezpieczeństwa.
                    </p>
                    {injections.length === 0 ? (
                        <div className="alert alert--success">
                            <p>Nie wykryto prób prompt injection w wybranym okresie.</p>
                        </div>
                    ) : (
                        <div className="table-wrap">
                            <table className="admin-table">
                                <thead>
                                    <tr>
                                        <th>Data wykrycia</th>
                                        <th>Kanał</th>
                                        <th>Źródło / lokalizacja</th>
                                        <th>Dosłowny fragment</th>
                                        <th>Detektor</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {injections.map((alert, index) => (
                                        <tr key={`${alert.run_id}-${index}`}>
                                            <td>{fmtDate(alert.detected_at)}</td>
                                            <td>{alert.channel}</td>
                                            <td>{alert.where ?? "–"}</td>
                                            <td className="admin-cell--wide">
                                                <code>{alert.excerpt}</code>
                                            </td>
                                            <td>{detectorLabel(alert.detected_by)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </section>
            )}
        </div>
    );
}