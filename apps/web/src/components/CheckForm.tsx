"use client";

import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent } from "react";

// Mirrors POST /check and POST /verify contracts in apps/backend/src/prawdowid/api/.
type InputType = "text" | "url" | "image" | "video";

type ClaimKind = "factual_claim" | "verification_question" | "opinion";
type Verdict = "confirmed" | "false" | "misleading" | "insufficient_evidence" | "unverifiable";
type Strength = "strong" | "moderate" | "weak";
type RunStatus = "pending" | "running" | "ok" | "tech_failure" | "needs_clarification" | "out_of_scope";
type ItemVerifyStatus = "verified" | "invalid_hash" | "missing_hash" | "unreadable";
type OverallVerifyStatus = "verified" | "partial" | "invalid_hash" | "missing_hash" | "unreadable";

type Source = {
  title: string;
  url: string;
  publisher: string;
  published_at: string | null;
  quote: string | null;
};

type Claim = {
  text: string;
  kind: ClaimKind;
  verdict: Verdict;
  strength: Strength;
  explanation: string;
  limitations: string | null;
  basis: "evidence" | "general_knowledge";
  sources: Source[];
  missing_details?: string[];
  answer_hash?: string | null;
};

type MediaInfo = {
  description: string;
  text: string;
  text_complete: boolean;
};

type CheckResponse = {
  run_id: string;
  status: RunStatus;
  message: string | null;
  claims: Claim[];
  media?: MediaInfo | null;
  skipped_claims?: string[];
  answer_hash?: string | null;
};

type VerifiedClaimItem = {
  claim_text: string;
  verdict: string | null;
  authentic: boolean;
  status: ItemVerifyStatus;
  message: string;
  extracted_hash: string | null;
};

type VerifyResponse = {
  authentic: boolean;
  status: OverallVerifyStatus;
  message: string;
  items?: VerifiedClaimItem[];
  extracted_hash?: string | null;
};

const MAX_FILE_BYTES = 12_000_000;

// Posts on these platforms cannot be read from a link; mirrors the API check.
const SOCIAL_PLATFORMS: Record<string, string> = {
  "x.com": "X",
  "twitter.com": "X",
  "t.co": "X",
  "tiktok.com": "TikTok",
  "instagram.com": "Instagram",
  "facebook.com": "Facebook",
  "fb.com": "Facebook",
  "fb.watch": "Facebook",
};

function socialPlatform(link: string): string | null {
  let host: string;
  try {
    host = new URL(link.trim()).hostname.toLowerCase();
  } catch {
    return null;
  }
  for (const [domain, name] of Object.entries(SOCIAL_PLATFORMS)) {
    if (host === domain || host.endsWith(`.${domain}`)) return name;
  }
  return null;
}

const ACCEPT: Record<"image" | "video", string[]> = {
  image: ["image/jpeg", "image/png", "image/webp"],
  video: ["video/mp4", "video/webm", "video/quicktime"],
};

const OPTIONS: { value: InputType; label: string; hint: string }[] = [
  { value: "text", label: "Tekst lub pytanie", hint: "wiadomość, wpis, cytat, pytanie" },
  { value: "url", label: "Link", hint: "artykuł, strona internetowa" },
  { value: "image", label: "Zdjęcie", hint: "zdjęcie lub zrzut ekranu" },
  { value: "video", label: "Wideo", hint: "nagranie wideo" },
];

const VERDICT_LABEL: Record<Verdict, string> = {
  confirmed: "Potwierdzone",
  false: "Fałszywe",
  misleading: "Wprowadzające w błąd",
  insufficient_evidence: "Brak wystarczających dowodów",
  unverifiable: "Nie podlega weryfikacji",
};

const VERDICT_STYLE: Record<Verdict, string> = {
  confirmed: "verdict--confirmed",
  false: "verdict--false",
  misleading: "verdict--misleading",
  insufficient_evidence: "verdict--unknown",
  unverifiable: "verdict--unknown",
};

const STRENGTH_LABEL: Record<Strength, string> = {
  strong: "Silna podstawa",
  moderate: "Umiarkowana podstawa",
  weak: "Słaba podstawa",
};

const CLAIM_KIND_LABEL: Record<ClaimKind, string> = {
  factual_claim: "Twierdzenie",
  verification_question: "Pytanie",
  opinion: "Opinia",
};

type LoadingMode = "check" | "verify-image";

type State =
  | { kind: "idle" }
  | { kind: "loading"; mode: LoadingMode }
  | { kind: "needs-clarification"; message: string | null; media: MediaInfo | null }
  | { kind: "out-of-scope"; message: string | null }
  | { kind: "busy"; message: string; retryAfter: number | null }
  | { kind: "invalid"; message: string }
  | { kind: "error"; message: string }
  | {
    kind: "done";
    message: string | null;
    media: MediaInfo | null;
    claims: Claim[];
    skipped: string[];
    answerHash: string | null;
  }
  | {
    kind: "verified-output";
    authentic: boolean;
    status: OverallVerifyStatus;
    message: string;
    items: VerifiedClaimItem[];
    extractedHash: string | null;
  };

function formatSize(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.max(1, Math.round(bytes / 1000))} kB`;
  return `${(bytes / 1_000_000).toLocaleString("pl-PL", { maximumFractionDigits: 1 })} MB`;
}

function validateFile(file: File, type: "image" | "video"): string | null {
  if (!ACCEPT[type].includes(file.type)) {
    return type === "image"
      ? "Wybierz zdjęcie w formacie JPG, PNG lub WebP."
      : "Wybierz nagranie w formacie MP4, WebM lub MOV.";
  }
  if (file.size > MAX_FILE_BYTES) {
    return "Plik jest za duży. Maksymalny rozmiar to 12 MB.";
  }
  return null;
}

// Must match the backend limits in prawdowid/services/preparation.py.
const MAX_TEXT_CHARS = 2000;
const MAX_QUESTION_CHARS = 500;
const MAX_URL_CHARS = 2000;

function charsLeftLabel(left: number): string {
  if (left === 1) return "Pozostał 1 znak";
  const lastDigit = left % 10;
  const lastTwo = left % 100;
  if (lastDigit >= 2 && lastDigit <= 4 && (lastTwo < 12 || lastTwo > 14)) return `Pozostały ${left} znaki`;
  return `Pozostało ${left} znaków`;
}

function CharCounter({ id, length, max }: { id: string; length: number; max: number }) {
  const left = Math.max(0, max - length);
  const nearLimit = left <= max * 0.1;
  return (
    <p id={id} className={`field__counter${nearLimit ? " field__counter--warn" : ""}`}>
      {charsLeftLabel(left)} (z {max})
    </p>
  );
}

type Draft = { text: string; url: string; file: File | null };

const EMPTY_DRAFT: Draft = { text: "", url: "", file: null };

function emptyDrafts(): Record<InputType, Draft> {
  return { text: EMPTY_DRAFT, url: EMPTY_DRAFT, image: EMPTY_DRAFT, video: EMPTY_DRAFT };
}

export function CheckForm() {
  const ids = useId();
  const [type, setType] = useState<InputType>("text");
  const [drafts, setDrafts] = useState<Record<InputType, Draft>>(emptyDrafts);
  const [fileError, setFileError] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: "idle" });
  const fileInput = useRef<HTMLInputElement>(null);

  const isMedia = type === "image" || type === "video";
  const { text, url, file } = drafts[type];
  const linkPlatform = type === "url" ? socialPlatform(url) : null;

  function updateDraft(change: Partial<Draft>) {
    setDrafts((current) => ({ ...current, [type]: { ...current[type], ...change } }));
  }

  // Pozwala wkleić zrzut ekranu ze schowka (Ctrl+V / Cmd+V) w dowolnym miejscu strony.
  useEffect(() => {
    function handleWindowPaste(event: ClipboardEvent) {
      const items = event.clipboardData?.items;
      if (!items) return;
      for (const item of Array.from(items)) {
        if (!item.type.startsWith("image/")) continue;
        const rawFile = item.getAsFile();
        if (!rawFile) continue;

        event.preventDefault();
        const ext = rawFile.type.split("/")[1] || "png";
        const pastedFile = rawFile.name
          ? rawFile
          : new File([rawFile], `zrzut-ekranu.${ext}`, { type: rawFile.type });
        const error = validateFile(pastedFile, "image");
        setType("image");
        setFileError(error);
        setState({ kind: "idle" });
        setDrafts((current) => ({
          ...current,
          image: { ...current.image, file: error ? null : pastedFile },
        }));
        break;
      }
    }

    window.addEventListener("paste", handleWindowPaste);
    return () => window.removeEventListener("paste", handleWindowPaste);
  }, []);

  useEffect(() => {
    if (!file || type !== "image") {
      setPreview(null);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setPreview(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file, type]);

  useEffect(() => {
    const input = fileInput.current;
    if (!input) return;
    if (file === null) {
      input.value = "";
    } else if (input.files?.[0] !== file) {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
    }
  }, [file, type]);

  function changeType(next: InputType) {
    setType(next);
    setFileError(null);
    setState({ kind: "idle" });
  }

  function onFileChange(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.target.files?.[0] ?? null;
    const error = chosen && isMedia ? validateFile(chosen, type) : null;
    setFileError(error);
    updateDraft({ file: error ? null : chosen });
    if (error) event.target.value = "";
  }

  function handleHttpError(response: Response, detail: string | null) {
    if (response.status === 429 || response.status === 503) {
      const retryAfter = Number(response.headers.get("retry-after"));
      setState({
        kind: "busy",
        message: detail ?? "Zbyt wiele zgłoszeń. Spróbuj ponownie za chwilę.",
        retryAfter: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
      });
    } else if ([413, 415, 422].includes(response.status)) {
      setState({ kind: "invalid", message: detail ?? "Sprawdź, czy wszystkie pola są poprawnie wypełnione." });
    } else {
      setState({ kind: "error", message: detail ?? "Usługa jest chwilowo niedostępna." });
    }
  }

  async function submitCheckRequest() {
    const body = new FormData();
    body.append("type", type);
    if (text.trim()) body.append("text", text.trim());
    if (type === "url") body.append("url", url.trim());
    if (isMedia && file) body.append("file", file);

    setState({ kind: "loading", mode: "check" });
    const response = await fetch("/api/check", { method: "POST", body });
    const payload = (await response.json().catch(() => ({}))) as Partial<CheckResponse> & { detail?: unknown };
    const detail = typeof payload.detail === "string" ? payload.detail : null;

    if (!response.ok) {
      handleHttpError(response, detail);
      return;
    }

    setDrafts(emptyDrafts());

    switch (payload.status) {
      case "ok":
        setState({
          kind: "done",
          message: payload.message ?? null,
          media: payload.media ?? null,
          claims: payload.claims ?? [],
          skipped: payload.skipped_claims ?? [],
          answerHash: payload.answer_hash ?? null,
        });
        break;
      case "needs_clarification":
        setState({ kind: "needs-clarification", message: payload.message ?? null, media: payload.media ?? null });
        break;
      case "out_of_scope":
        setState({ kind: "out-of-scope", message: payload.message ?? null });
        break;
      case "tech_failure":
      default:
        setState({
          kind: "error",
          message: payload.message ?? "Nie udało się sprawdzić zgłoszenia z powodu błędu technicznego.",
        });
    }
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state.kind === "loading") return;
    if (linkPlatform) return;
    if (isMedia && !file) {
      setFileError(type === "image" ? "Wybierz zdjęcie lub wklej zrzut ekranu." : "Wybierz nagranie.");
      return;
    }

    try {
      // Obraz -> automatycznie sprawdzamy, czy zawiera co najmniej jeden hash Prawdowida.
      // Jeśli tak (verified / partial / invalid_hash), pokazujemy wynik weryfikacji każdego kafelka.
      // Jeśli nie (zwykłe zdjęcie), przechodzimy do standardowego /api/check.
      if (type === "image" && file) {
        const verifyBody = new FormData();
        verifyBody.append("file", file);

        setState({ kind: "loading", mode: "verify-image" });
        const verifyResponse = await fetch("/api/verify", { method: "POST", body: verifyBody });
        const verifyPayload = (await verifyResponse.json().catch(() => ({}))) as Partial<VerifyResponse> & {
          detail?: unknown;
        };
        const verifyDetail = typeof verifyPayload.detail === "string" ? verifyPayload.detail : null;

        if (verifyResponse.ok) {
          if (
            verifyPayload.status === "verified" ||
            verifyPayload.status === "partial" ||
            verifyPayload.status === "invalid_hash"
          ) {
            setDrafts(emptyDrafts());
            setState({
              kind: "verified-output",
              authentic: Boolean(verifyPayload.authentic),
              status: verifyPayload.status,
              message: verifyPayload.message ?? "Zakończono weryfikację zrzutu ekranu.",
              items: verifyPayload.items ?? [],
              extractedHash: verifyPayload.extracted_hash ?? null,
            });
            return;
          }
        } else if (verifyResponse.status === 503) {
          // Brak skonfigurowanego PRAWDOWID_SIGNING_KEY nie blokuje zwykłego sprawdzania zdjęć.
        } else {
          handleHttpError(verifyResponse, verifyDetail);
          return;
        }
      }

      await submitCheckRequest();
    } catch {
      setState({ kind: "error", message: "Nie można połączyć się z serwerem strony." });
    }
  }

  const textMax = type === "text" ? MAX_TEXT_CHARS : MAX_QUESTION_CHARS;
  const textLabel =
    type === "text" ? "Treść lub pytanie do sprawdzenia" : "Pytanie (opcjonalnie)";
  const textHint =
    type === "text"
      ? "Wklej wiadomość, wpis lub cytat albo zadaj pytanie, na które da się odpowiedzieć „tak” lub „nie”, np. „Czy we Włoszech ostatnio odbywał się strajk generalny?”. Nie podawaj danych osobowych."
      : type === "image"
        ? "Co dokładnie chcesz sprawdzić? Jeśli wklejasz zrzut ekranu odpowiedzi Prawdowida z hashami, zweryfikujemy każdy kafelek osobno."
        : type === "video"
          ? "Co dokładnie chcesz sprawdzić? Na przykład: Czy to nagranie pokazuje tegoroczny protest?"
          : "Co dokładnie chcesz sprawdzić? Na przykład: Czy ten artykuł podaje prawdziwe dane?";

  return (
    <>
      <form className="form" onSubmit={onSubmit}>
        <fieldset className="choice">
          <legend className="field__label">Co chcesz sprawdzić?</legend>
          <div className="choice__options">
            {OPTIONS.map((option) => (
              <label key={option.value} className="choice__option">
                <input
                  type="radio"
                  name="type"
                  value={option.value}
                  checked={type === option.value}
                  onChange={() => changeType(option.value)}
                />
                <span className="choice__label">{option.label}</span>
                <span className="choice__hint">{option.hint}</span>
              </label>
            ))}
          </div>
        </fieldset>

        {type === "url" && (
          <div className="field">
            <label htmlFor={`${ids}-url`} className="field__label">
              Link
            </label>
            <p id={`${ids}-url-hint`} className="field__hint">
              Adres artykułu lub strony, np. https://example.org/artykul
            </p>
            <input
              id={`${ids}-url`}
              type="url"
              inputMode="url"
              required
              maxLength={MAX_URL_CHARS}
              value={url}
              onChange={(event) => updateDraft({ url: event.target.value })}
              aria-describedby={`${ids}-url-hint ${ids}-url-count${linkPlatform ? ` ${ids}-url-social` : ""}`}
              aria-invalid={linkPlatform ? true : undefined}
              className="input"
            />
            <CharCounter id={`${ids}-url-count`} length={url.length} max={MAX_URL_CHARS} />
            {linkPlatform && (
              <div id={`${ids}-url-social`} className="alert alert--warning social-link" role="alert">
                <p className="alert__title">Wpisu z serwisu {linkPlatform} nie odczytam z linku</p>
                <p>Zrób zrzut ekranu wpisu albo pobierz nagranie i wrzuć plik.</p>
                <div className="social-link__actions">
                  <button type="button" className="button button--secondary" onClick={() => changeType("image")}>
                    Wrzuć zrzut ekranu
                  </button>
                  <button type="button" className="button button--secondary" onClick={() => changeType("video")}>
                    Wrzuć wideo
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {isMedia && (
          <div className="field">
            <label htmlFor={`${ids}-file`} className="field__label">
              {type === "image" ? "Zdjęcie lub zrzut ekranu" : "Nagranie wideo"}
            </label>
            <p id={`${ids}-file-hint`} className="field__hint">
              {type === "image"
                ? "JPG, PNG lub WebP, maksymalnie 12 MB. Wybierz plik albo wklej zrzut ekranu ze schowka (Ctrl+V / Cmd+V)."
                : "MP4, WebM lub MOV, maksymalnie 12 MB."}
            </p>
            {fileError && (
              <p id={`${ids}-file-error`} className="field__error">
                {fileError}
              </p>
            )}
            <input
              id={`${ids}-file`}
              key={type}
              ref={fileInput}
              type="file"
              accept={ACCEPT[type].join(",")}
              onChange={onFileChange}
              aria-describedby={`${ids}-file-hint${fileError ? ` ${ids}-file-error` : ""}`}
              aria-invalid={fileError ? true : undefined}
              className="file-input"
            />
            {file && (
              <div className="file-summary">
                {preview && <img src={preview} alt="Podgląd wybranego zdjęcia" className="file-summary__preview" />}
                <span>
                  {file.name} ({formatSize(file.size)})
                </span>
              </div>
            )}
          </div>
        )}

        <div className="field">
          <label htmlFor={`${ids}-text`} className="field__label">
            {textLabel}
          </label>
          <p id={`${ids}-text-hint`} className="field__hint">
            {textHint}
          </p>
          <textarea
            id={`${ids}-text`}
            rows={type === "text" ? 5 : 3}
            maxLength={textMax}
            required={type === "text"}
            value={text}
            onChange={(event) => updateDraft({ text: event.target.value })}
            aria-describedby={`${ids}-text-hint ${ids}-text-count`}
            className="input"
          />
          <CharCounter id={`${ids}-text-count`} length={text.length} max={textMax} />
        </div>

        {state.kind === "loading" ? (
          <SearchProgress mode={state.mode} />
        ) : (
          <button type="submit" className="button">
            Sprawdź
          </button>
        )}
      </form>

      <div aria-live="polite" className="result-region">
        <Result state={state} />
      </div>
    </>
  );
}

const SEARCH_STAGES = [
  "Wyodrębniam twierdzenia do sprawdzenia",
  "Przeszukuję bazy fact-checkerów",
  "Szukam w analizach dezinformacji",
  "Przeglądam źródła rządowe",
  "Porównuję dowody z twierdzeniami",
  "Przygotowuję odpowiedź ze źródłami",
];
const STAGE_INTERVAL_MS = 3500;

function SearchProgress({ mode }: { mode: LoadingMode }) {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    if (mode !== "check") return;
    setStage(0);
    const timer = setInterval(
      () => setStage((current) => Math.min(current + 1, SEARCH_STAGES.length - 1)),
      STAGE_INTERVAL_MS,
    );
    return () => clearInterval(timer);
  }, [mode]);

  if (mode === "verify-image") {
    return (
      <div className="search-progress" role="status">
        <p className="search-progress__single">
          Analizuję obraz i sprawdzam podpisy odpowiedzi Prawdowida…
        </p>
      </div>
    );
  }

  return (
    <div className="search-progress" role="status">
      <span className="sr-only">Sprawdzanie może potrwać kilkadziesiąt sekund.</span>
      <svg className="search-progress__art" viewBox="0 0 120 90" aria-hidden="true">
        <rect x="14" y="8" width="72" height="74" rx="6" className="search-progress__page" />
        <rect x="24" y="22" width="46" height="5" rx="2.5" className="search-progress__line search-progress__line--1" />
        <rect x="24" y="36" width="52" height="5" rx="2.5" className="search-progress__line search-progress__line--2" />
        <rect x="24" y="50" width="40" height="5" rx="2.5" className="search-progress__line search-progress__line--3" />
        <rect x="24" y="64" width="48" height="5" rx="2.5" className="search-progress__line search-progress__line--4" />
        <g className="search-progress__lens">
          <circle cx="0" cy="0" r="14" className="search-progress__glass" />
          <line x1="10" y1="10" x2="24" y2="24" className="search-progress__handle" />
        </g>
      </svg>
      <ol className="search-progress__stages" aria-hidden="true">
        {SEARCH_STAGES.map((label, index) => (
          <li
            key={label}
            className={
              index < stage
                ? "search-progress__stage search-progress__stage--done"
                : index === stage
                  ? "search-progress__stage search-progress__stage--active"
                  : "search-progress__stage"
            }
          >
            {label}
          </li>
        ))}
      </ol>
    </div>
  );
}

function MediaReadout({ media }: { media: MediaInfo }) {
  return (
    <section className="media-readout" aria-label="Co odczytano z pliku">
      {media.description && (
        <p className="media-readout__description">
          <strong>Na pliku:</strong> {media.description}
        </p>
      )}
      {media.text ? (
        <details className="media-readout__text" open>
          <summary>Odczytany tekst</summary>
          <p>{media.text}</p>
        </details>
      ) : (
        <p className="media-readout__empty">Nie odczytano żadnego tekstu.</p>
      )}
      {!media.text_complete && (
        <p className="media-readout__warning">
          Części tekstu nie udało się odczytać w całości. Sprawdź, czy niczego nie brakuje.
        </p>
      )}
    </section>
  );
}

function Result({ state }: { state: State }) {
  switch (state.kind) {
    case "invalid":
      return (
        <div className="alert alert--error">
          <p className="alert__title">Popraw zgłoszenie</p>
          <p>{state.message}</p>
        </div>
      );
    case "busy":
      return (
        <div className="alert alert--warning">
          <p className="alert__title">Limit zgłoszeń</p>
          <p>{state.message}</p>
          {state.retryAfter !== null && <p>Spróbuj ponownie za około {state.retryAfter} s.</p>}
        </div>
      );
    case "out-of-scope":
      return (
        <div className="alert alert--warning">
          <p className="alert__title">Nie mogę tego ocenić automatycznie</p>
          <p>{state.message ?? "Tej treści nie da się przeanalizować automatycznie. To nie jest ocena prawdziwości."}</p>
        </div>
      );
    case "needs-clarification":
      return (
        <div className="results">
          {state.media && <MediaReadout media={state.media} />}
          <div className="alert alert--info">
            <p className="alert__title">Doprecyzuj zgłoszenie</p>
            <p>
              {state.message ??
                "Nie udało się odczytać treści do sprawdzenia. Opisz dokładniej, co chcesz zweryfikować."}
            </p>
          </div>
        </div>
      );
    case "error":
      return (
        <div className="alert alert--error">
          <p className="alert__title">Błąd techniczny</p>
          <p>{state.message ?? "Usługa jest chwilowo niedostępna. Spróbuj ponownie później."}</p>
        </div>
      );
    case "verified-output": {
      const alertClass =
        state.status === "verified"
          ? "alert--success"
          : state.status === "partial"
            ? "alert--warning"
            : "alert--error";
      const title =
        state.status === "verified"
          ? "Odpowiedź autentyczna – wygenerowana przez Prawdowid"
          : state.status === "partial"
            ? "Częściowo potwierdzona odpowiedź Prawdowida"
            : "Odpowiedź nieautentyczna lub zmodyfikowana";

      return (
        <div className="results">
          <div className={`alert ${alertClass} verify-result`}>
            <p className="alert__title">{title}</p>
            <p>{state.message}</p>
            {state.items.length <= 1 && state.extractedHash && (
              <p className="verify-result__hash">
                <strong>Odczytany hash:</strong> <code>{state.extractedHash}</code>
              </p>
            )}
          </div>

          {state.items.length > 1 && (
            <ul className="verify-items">
              {state.items.map((item, index) => {
                const itemMod = item.authentic
                  ? "verify-item--ok"
                  : item.status === "missing_hash"
                    ? "verify-item--warn"
                    : "verify-item--bad";
                const badgeLabel = item.authentic
                  ? "Autentyczne"
                  : item.status === "missing_hash"
                    ? "Brak hasha"
                    : "Nieprawidłowy hash";

                return (
                  <li key={`${item.claim_text}-${index}`} className={`verify-item ${itemMod}`}>
                    <div className="verify-item__header">
                      <span className="verify-item__badge">{badgeLabel}</span>
                      {item.verdict && <span className="verify-item__verdict">{item.verdict}</span>}
                    </div>
                    <p className="verify-item__title">{item.claim_text}</p>
                    <p className="verify-item__message">{item.message}</p>
                    {item.extracted_hash && (
                      <p className="verify-result__hash">
                        <code>{item.extracted_hash}</code>
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      );
    }
    case "done":
      if (state.claims.length === 0) {
        return (
          <div className="results">
            {state.media && <MediaReadout media={state.media} />}
            <div className="alert alert--info">
              <div className="alert__header">
                <p className="alert__title">Brak twierdzeń do sprawdzenia</p>
                {state.answerHash && <HashBadge hash={state.answerHash} />}
              </div>
              <p>{state.message ?? "Nie znaleziono w zgłoszeniu żadnej tezy wymagającej weryfikacji."}</p>
            </div>
          </div>
        );
      }
      return (
        <div className="results">
          {state.media && <MediaReadout media={state.media} />}
          {state.message && (
            <div className="alert alert--info">
              <p>{state.message}</p>
            </div>
          )}
          {state.claims.map((claim, index) => (
            <ClaimResult key={`${claim.text}-${index}`} claim={claim} />
          ))}
          {state.skipped.length > 0 && (
            <details className="skipped-claims">
              <summary>
                Pominięte poboczne twierdzenia ({state.skipped.length}) – sprawdziłem tylko te kluczowe dla
                Twojego pytania
              </summary>
              <ul>
                {state.skipped.map((text, index) => (
                  <li key={`${text}-${index}`}>{text}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      );
    default:
      return null;
  }
}

function HashBadge({ hash }: { hash: string }) {
  const tooltipId = useId();
  return (
    <span className="answer-hash">
      <code className="answer-hash__code">{hash}</code>
      <span className="answer-hash__help">
        <button
          type="button"
          className="answer-hash__help-button"
          aria-label="Co oznacza ten podpis?"
          aria-describedby={tooltipId}
        >
          ?
        </button>
        <span id={tooltipId} role="tooltip" className="answer-hash__tooltip">
          Kryptograficzny podpis tego stwierdzenia. Prześlij lub wklej zrzut ekranu (np. Ctrl+V) w polu zdjęcia
          powyżej, aby potwierdzić, że treść kafelka nie została zmieniona.
        </span>
      </span>
    </span>
  );
}

function ClaimResult({ claim }: { claim: Claim }) {
  return (
    <article className="claim-result">
      <p className="claim-result__kind">{CLAIM_KIND_LABEL[claim.kind]}</p>
      <h3 className="claim-result__text">{claim.text}</h3>
      <div className="claim-result__badges">
        <span className={`verdict ${VERDICT_STYLE[claim.verdict]}`}>{VERDICT_LABEL[claim.verdict]}</span>
        {claim.answer_hash && <HashBadge hash={claim.answer_hash} />}
        <span className="strength">{STRENGTH_LABEL[claim.strength]}</span>
        {claim.basis === "general_knowledge" && (
          <span className="basis-badge">Wiedza ogólna – bez źródła</span>
        )}
      </div>
      <p className="claim-result__explanation">{claim.explanation}</p>
      {claim.missing_details && claim.missing_details.length > 0 && (
        <div className="missing-details">
          <p className="missing-details__title">Doprecyzuj, aby to sprawdzić:</p>
          <ul>
            {claim.missing_details.map((detail, index) => (
              <li key={`${detail}-${index}`}>{detail}</li>
            ))}
          </ul>
        </div>
      )}
      {claim.limitations && (
        <p className="claim-result__limitations">
          <strong>Ograniczenia:</strong> {claim.limitations}
        </p>
      )}
      {claim.sources.length > 0 && (
        <div className="sources">
          <p className="sources__title">Źródła</p>
          <ul className="sources__list">
            {claim.sources.map((source, index) => (
              <li key={`${source.url}-${index}`} className="source">
                <a href={source.url} target="_blank" rel="noreferrer noopener" className="source__title">
                  {source.title}
                </a>
                <span className="source__publisher"> – {source.publisher}</span>
                {source.published_at && (
                  <time className="source__date" dateTime={source.published_at}>
                    {", "}
                    {new Date(source.published_at).toLocaleDateString("pl-PL")}
                  </time>
                )}
                {source.quote && <blockquote className="source__quote">„{source.quote}”</blockquote>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}
