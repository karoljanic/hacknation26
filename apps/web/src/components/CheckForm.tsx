"use client";

import { useState, type FormEvent } from "react";

import type { StatusResponse } from "../app/api/status/route";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "done"; result: StatusResponse };

export function CheckForm() {
  const [state, setState] = useState<State>({ kind: "idle" });

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState({ kind: "loading" });
    // TODO: temporary. Replace with a call to the verification endpoint once
    // the backend has one; the entered text/link is not sent anywhere yet.
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      setState({ kind: "done", result: (await response.json()) as StatusResponse });
    } catch {
      setState({
        kind: "done",
        result: { ok: false, error: "Nie można połączyć się z serwerem strony.", checkedAt: new Date().toISOString() },
      });
    }
  }

  return (
    <>
      <div className="alert alert--warning" role="note">
        <p className="alert__title">Wersja tymczasowa</p>
        <p>
          Sprawdzanie informacji nie jest jeszcze dostępne. Przycisk „Sprawdź” na razie tylko
          sprawdza, czy usługa i baza danych działają. Wpisana treść nie jest nigdzie wysyłana.
          To zachowanie zostanie zmienione w kolejnej wersji.
        </p>
      </div>

      <form className="form" onSubmit={onSubmit}>
        <div className="field">
          <label htmlFor="claim-text" className="field__label">
            Treść do sprawdzenia
          </label>
          <p id="claim-text-hint" className="field__hint">
            Wklej wiadomość, wpis lub cytat. Nie podawaj danych osobowych.
          </p>
          <textarea
            id="claim-text"
            name="text"
            rows={5}
            aria-describedby="claim-text-hint"
            className="input"
          />
        </div>

        <div className="field">
          <label htmlFor="claim-url" className="field__label">
            Link (opcjonalnie)
          </label>
          <p id="claim-url-hint" className="field__hint">
            Adres artykułu lub wpisu, np. https://example.org/artykul
          </p>
          <input
            id="claim-url"
            name="url"
            type="url"
            inputMode="url"
            aria-describedby="claim-url-hint"
            className="input"
          />
        </div>

        <button type="submit" className="button" disabled={state.kind === "loading"}>
          {state.kind === "loading" ? "Sprawdzanie…" : "Sprawdź"}
        </button>
      </form>

      <div aria-live="polite" className="result-region">
        {state.kind === "done" && <StatusResult result={state.result} />}
      </div>
    </>
  );
}

function StatusResult({ result }: { result: StatusResponse }) {
  const time = new Date(result.checkedAt).toLocaleTimeString("pl-PL");
  if (result.ok) {
    return (
      <div className="alert alert--success">
        <p className="alert__title">Usługa działa</p>
        <p>
          Połączenie z bazą danych jest poprawne (wersja schematu: {result.dbRevision}). Sprawdzono
          o {time}.
        </p>
      </div>
    );
  }
  return (
    <div className="alert alert--error">
      <p className="alert__title">Usługa jest niedostępna</p>
      <p>
        {result.error} Sprawdzono o {time}. Spróbuj ponownie później.
      </p>
    </div>
  );
}
