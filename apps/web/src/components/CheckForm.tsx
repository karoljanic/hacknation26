"use client";

import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent } from "react";

// Mirrors the backend's POST /check contract (apps/backend/src/prawdowid/api/check.py).
type InputType = "text" | "question" | "url" | "image" | "video";

const MAX_FILE_BYTES = 12_000_000;

const ACCEPT: Record<"image" | "video", string[]> = {
  image: ["image/jpeg", "image/png", "image/webp"],
  video: ["video/mp4", "video/webm", "video/quicktime"],
};

const OPTIONS: { value: InputType; label: string; hint: string }[] = [
  { value: "text", label: "Tekst", hint: "wiadomość, wpis, cytat" },
  { value: "url", label: "Link", hint: "artykuł, wpis, nagranie" },
  { value: "question", label: "Pytanie", hint: "zadaj pytanie" },
  { value: "image", label: "Zdjęcie", hint: "zdjęcie lub zrzut ekranu" },
  { value: "video", label: "Wideo", hint: "nagranie wideo" },
];

type Claim = { text: string; verdict: string; explanation: string };

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "not-implemented" }
  | { kind: "invalid"; message: string }
  | { kind: "error"; message: string }
  | { kind: "done"; message: string | null; claims: Claim[] };

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

// Each input type keeps its own values: switching type shows that type's
// fields (empty at first) and switching back restores what was entered.
type Draft = { text: string; url: string; file: File | null };

const EMPTY_DRAFT: Draft = { text: "", url: "", file: null };

function emptyDrafts(): Record<InputType, Draft> {
  return { text: EMPTY_DRAFT, question: EMPTY_DRAFT, url: EMPTY_DRAFT, image: EMPTY_DRAFT, video: EMPTY_DRAFT };
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

  function updateDraft(change: Partial<Draft>) {
    setDrafts((current) => ({ ...current, [type]: { ...current[type], ...change } }));
  }

  useEffect(() => {
    if (!file || type !== "image") {
      setPreview(null);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setPreview(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file, type]);

  // A file input cannot be controlled by React: put the saved file back when
  // returning to this type, and empty it when the drafts are cleared.
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

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isMedia && !file) {
      setFileError(type === "image" ? "Wybierz zdjęcie." : "Wybierz nagranie.");
      return;
    }

    const body = new FormData();
    body.append("type", type);
    if (text.trim()) body.append("text", text.trim());
    if (type === "url") body.append("url", url.trim());
    if (isMedia && file) body.append("file", file);

    setState({ kind: "loading" });
    try {
      const response = await fetch("/api/check", { method: "POST", body });
      const payload = (await response.json().catch(() => ({}))) as {
        detail?: unknown;
        message?: string | null;
        claims?: Claim[];
      };
      const detail = typeof payload.detail === "string" ? payload.detail : null;

      // Sent and accepted: clear every type's fields. On a validation or
      // technical error they stay, so the request can be corrected or resent.
      if (response.ok || response.status === 501) {
        setDrafts(emptyDrafts());
      }

      if (response.ok) {
        setState({ kind: "done", message: payload.message ?? null, claims: payload.claims ?? [] });
      } else if (response.status === 501) {
        setState({ kind: "not-implemented" });
      } else if ([413, 415, 422].includes(response.status)) {
        setState({ kind: "invalid", message: detail ?? "Sprawdź, czy wszystkie pola są poprawnie wypełnione." });
      } else {
        setState({ kind: "error", message: detail ?? "Usługa jest chwilowo niedostępna." });
      }
    } catch {
      setState({ kind: "error", message: "Nie można połączyć się z serwerem strony." });
    }
  }

  const textLabel =
    type === "text" ? "Treść do sprawdzenia" : type === "question" ? "Twoje pytanie" : "Pytanie (opcjonalnie)";
  const textHint =
    type === "text"
      ? "Wklej wiadomość, wpis lub cytat. Nie podawaj danych osobowych."
      : type === "question"
        ? "Na przykład: Czy we Włoszech ostatnio odbywał się strajk generalny?"
        : type === "image"
          ? "Co dokładnie chcesz sprawdzić? Na przykład: Czy to zdjęcie jest aktualne?"
          : type === "video"
            ? "Co dokładnie chcesz sprawdzić? Na przykład: Czy to nagranie pokazuje tegoroczny protest?"
            : "Co dokładnie chcesz sprawdzić? Na przykład: Czy ten artykuł podaje prawdziwe dane?";

  return (
    <>
      <div className="alert alert--warning" role="note">
        <p className="alert__title">Wersja tymczasowa</p>
        <p>
          Sprawdzanie informacji nie jest jeszcze dostępne. Formularz przesyła zgłoszenie do
          usługi, która na razie tylko sprawdza jego poprawność i odpowiada, że ta funkcja nie jest
          gotowa. Zgłoszenia nie są zapisywane.
        </p>
      </div>

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
              Adres artykułu, wpisu lub nagrania, np. https://example.org/artykul
            </p>
            <input
              id={`${ids}-url`}
              type="url"
              inputMode="url"
              required
              value={url}
              onChange={(event) => updateDraft({ url: event.target.value })}
              aria-describedby={`${ids}-url-hint`}
              className="input"
            />
          </div>
        )}

        {isMedia && (
          <div className="field">
            <label htmlFor={`${ids}-file`} className="field__label">
              {type === "image" ? "Zdjęcie lub zrzut ekranu" : "Nagranie wideo"}
            </label>
            <p id={`${ids}-file-hint`} className="field__hint">
              {type === "image" ? "JPG, PNG lub WebP" : "MP4, WebM lub MOV"}, maksymalnie 12 MB.
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
            maxLength={5000}
            required={type === "text" || type === "question"}
            value={text}
            onChange={(event) => updateDraft({ text: event.target.value })}
            aria-describedby={`${ids}-text-hint`}
            className="input"
          />
        </div>

        <button type="submit" className="button" disabled={state.kind === "loading"}>
          {state.kind === "loading" ? "Wysyłanie…" : "Sprawdź"}
        </button>
      </form>

      <div aria-live="polite" className="result-region">
        <Result state={state} />
      </div>
    </>
  );
}

function Result({ state }: { state: State }) {
  switch (state.kind) {
    case "not-implemented":
      return (
        <div className="alert alert--info">
          <p className="alert__title">Zgłoszenie jest poprawne</p>
          <p>Usługa przyjęła dane, ale samo sprawdzanie nie jest jeszcze dostępne.</p>
        </div>
      );
    case "invalid":
      return (
        <div className="alert alert--error">
          <p className="alert__title">Popraw zgłoszenie</p>
          <p>{state.message}</p>
        </div>
      );
    case "error":
      return (
        <div className="alert alert--error">
          <p className="alert__title">Błąd techniczny</p>
          <p>{state.message} Spróbuj ponownie później.</p>
        </div>
      );
    case "done":
      return (
        <div className="alert alert--success">
          <p className="alert__title">Wynik</p>
          {state.message && <p>{state.message}</p>}
          {state.claims.map((claim) => (
            <p key={claim.text}>
              <strong>{claim.text}</strong> – {claim.verdict}. {claim.explanation}
            </p>
          ))}
        </div>
      );
    default:
      return null;
  }
}
