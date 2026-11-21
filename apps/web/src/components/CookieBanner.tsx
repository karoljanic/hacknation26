"use client";

import { useEffect, useState } from "react";

// The site currently sets no cookies and loads no analytics or third-party
// scripts. The banner records the visitor's choice so that anything optional
// added later (e.g. analytics) can be loaded only when `hasOptionalConsent()`
// returns true. Rejecting is as easy as accepting, as EU guidance requires.

const STORAGE_KEY = "prawdowid-cookie-consent";
const OPEN_EVENT = "prawdowid:open-cookie-settings";

export type CookieConsent = "all" | "necessary";

function readConsent(): CookieConsent | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "all" || value === "necessary" ? value : null;
  } catch {
    return null;
  }
}

export function hasOptionalConsent(): boolean {
  return readConsent() === "all";
}

export function openCookieSettings(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

export function CookieBanner() {
  // Hidden until mounted: the server cannot know the stored choice.
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setOpen(readConsent() === null);
    const reopen = () => setOpen(true);
    window.addEventListener(OPEN_EVENT, reopen);
    return () => window.removeEventListener(OPEN_EVENT, reopen);
  }, []);

  function choose(consent: CookieConsent) {
    try {
      window.localStorage.setItem(STORAGE_KEY, consent);
    } catch {
      // Storage blocked (private mode etc.): the choice applies to this visit only.
    }
    setOpen(false);
  }

  if (!open) return null;

  return (
    <section className="cookie-banner" aria-labelledby="cookie-banner-title" role="region">
      <div className="container cookie-banner__inner">
        <div>
          <h2 id="cookie-banner-title" className="cookie-banner__title">
            Pliki cookies
          </h2>
          <p>
            Używamy wyłącznie mechanizmów niezbędnych do działania strony. Za Twoją zgodą możemy w
            przyszłości używać także plików cookies analitycznych, aby ulepszać serwis. Szczegóły
            znajdziesz w <a href="/polityka-prywatnosci">Polityce prywatności</a>. Wybór możesz
            zmienić w każdej chwili w stopce strony.
          </p>
        </div>
        <div className="cookie-banner__actions">
          <button type="button" className="button" onClick={() => choose("all")}>
            Akceptuję wszystkie
          </button>
          <button type="button" className="button" onClick={() => choose("necessary")}>
            Tylko niezbędne
          </button>
        </div>
      </div>
    </section>
  );
}

export function CookieSettingsButton() {
  return (
    <button type="button" className="link-button" onClick={openCookieSettings}>
      Ustawienia cookies
    </button>
  );
}
