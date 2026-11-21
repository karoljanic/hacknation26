import type { ReactNode } from "react";

import { CookieBanner, CookieSettingsButton } from "../components/CookieBanner";
import "./globals.css";

export const metadata = {
  title: "Sprawdź informację – Prawdowid",
  description: "Narzędzie do weryfikacji informacji",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="pl">
      <body>
        <a href="#main" className="skip-link">
          Przejdź do treści
        </a>

        <header className="header">
          <div className="container header__inner">
            <a href="/" className="brand">
              {/* Project mark only: not the national emblem or gov.pl branding. */}
              <svg className="brand__mark" viewBox="0 0 32 32" aria-hidden="true">
                <rect x="1" y="1" width="30" height="30" rx="4" fill="#fff" stroke="currentColor" strokeWidth="2" />
                <path d="M9 16.5l4.5 4.5L23 11" fill="none" stroke="#d4213d" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className="brand__text">
                <span className="brand__name">Prawdowid</span>
                <span className="brand__tagline">Weryfikacja informacji</span>
              </span>
            </a>
          </div>
        </header>

        <div className="notice-bar">
          <div className="container">
            Prototyp przygotowany w ramach hackathonu. To nie jest oficjalny serwis administracji
            publicznej.
          </div>
        </div>

        {children}

        <footer className="footer">
          <div className="container">
            <p>Prawdowid – prototyp narzędzia do weryfikacji informacji.</p>
            <p>Odpowiedzi opierają się na wskazanych źródłach. Zawsze możesz zapoznać się z nimi samodzielnie.</p>
            <ul className="footer__links">
              <li>
                <a href="/polityka-prywatnosci">Polityka prywatności</a>
              </li>
              <li>
                <CookieSettingsButton />
              </li>
            </ul>
          </div>
        </footer>

        <CookieBanner />
      </body>
    </html>
  );
}
