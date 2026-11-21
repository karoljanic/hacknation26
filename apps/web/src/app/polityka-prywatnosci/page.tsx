import type { ReactNode } from "react";

export const metadata = {
  title: "Polityka prywatności – Prawdowid",
};

// TEMPLATE: fill every <Placeholder> and have the text reviewed by a lawyer /
// the data protection officer before publishing. Facts about processing below
// mirror the current backend (apps/backend: services/intake.py,
// security/redaction.py, persistence/models.py); update them when it changes.

function Placeholder({ children }: { children: ReactNode }) {
  return <mark className="placeholder">[{children}]</mark>;
}

export default function PrivacyPolicy() {
  return (
    <main id="main" className="container main">
      <nav aria-label="Ścieżka nawigacji" className="breadcrumbs">
        <ol>
          <li>
            <a href="/">Prawdowid</a>
          </li>
          <li aria-current="page">Polityka prywatności</li>
        </ol>
      </nav>

      <article className="prose">
        <h1>Polityka prywatności</h1>

        <div className="alert alert--warning" role="note">
          <p className="alert__title">Szablon dokumentu</p>
          <p>
            To robocza wersja polityki prywatności. Pola oznaczone [w nawiasach] wymagają
            uzupełnienia, a całość – weryfikacji prawnej przed publikacją.
          </p>
        </div>

        <p>Ostatnia aktualizacja: <Placeholder>data</Placeholder></p>

        <h2>1. Administrator danych</h2>
        <p>
          Administratorem danych osobowych przetwarzanych w serwisie Prawdowid jest{" "}
          <Placeholder>nazwa administratora</Placeholder>, z siedzibą w{" "}
          <Placeholder>adres</Placeholder>. Kontakt: <Placeholder>adres e-mail</Placeholder>.
        </p>
        <p>
          Inspektor ochrony danych: <Placeholder>imię i nazwisko lub nazwa, adres e-mail</Placeholder>.
        </p>

        <h2>2. Jakie dane przetwarzamy</h2>
        <p>Gdy przesyłasz zgłoszenie do sprawdzenia, zapisujemy:</p>
        <ul>
          <li>
            treść zgłoszenia, po automatycznym zamaskowaniu oczywistych danych osobowych (adresów
            e-mail, numerów telefonów, numerów PESEL, numerów kont i kart płatniczych);
          </li>
          <li>przesłany link (zapisujemy sam adres, nie kopię artykułu);</li>
          <li>kanał zgłoszenia (np. strona WWW) oraz datę i godzinę;</li>
          <li>
            w kanałach komunikatorów – skrót (hash) identyfikatora nadawcy, który nie pozwala
            odczytać samego identyfikatora; służy do ograniczania nadużyć;
          </li>
          <li>
            dane techniczne przebiegu sprawdzenia: czas odpowiedzi, użyte modele, ewentualne błędy.
          </li>
        </ul>
        <p>
          Maskowanie jest automatyczne i nie rozpoznaje wszystkich danych (np. imion, nazwisk czy
          adresów). <strong>Nie podawaj w zgłoszeniach danych osobowych</strong> – swoich ani innych
          osób.
        </p>

        <h2>3. Cel i podstawa prawna</h2>
        <p>
          Dane przetwarzamy, aby sprawdzić przesłaną informację i udzielić odpowiedzi, a także
          aby zapewnić bezpieczeństwo i poprawne działanie serwisu. Podstawa prawna:{" "}
          <Placeholder>np. art. 6 ust. 1 lit. e RODO – zadanie realizowane w interesie publicznym, wraz z przepisem krajowym</Placeholder>.
        </p>

        <h2>4. Odbiorcy danych</h2>
        <p>
          Do sprawdzenia zgłoszenia zamaskowana treść jest przekazywana dostawcom usług, którzy
          przetwarzają ją w naszym imieniu:
        </p>
        <ul>
          <li>Google (model językowy Gemini) – analiza treści i wyodrębnienie twierdzeń;</li>
          <li>
            serwisy weryfikacji faktów (np. Google Fact Check Tools, EUvsDisinfo) – wyszukiwanie
            wcześniejszych weryfikacji;
          </li>
          <li>
            <Placeholder>dostawca hostingu i pozostali dostawcy</Placeholder>.
          </li>
        </ul>
        <p>
          Przekazywanie danych poza Europejski Obszar Gospodarczy:{" "}
          <Placeholder>opis i podstawa przekazania, np. standardowe klauzule umowne</Placeholder>.
        </p>

        <h2>5. Jak długo przechowujemy dane</h2>
        <p>
          Zgłoszenia i dane o ich przebiegu przechowujemy przez{" "}
          <Placeholder>30 dni</Placeholder>, a następnie je usuwamy. Twierdzenia wyodrębnione ze
          zgłoszeń, pozbawione powiązania ze zgłoszeniem, mogą być przechowywane dłużej, aby szybciej
          odpowiadać na powtarzające się pytania. <Placeholder>potwierdzić okres przechowywania</Placeholder>
        </p>

        <h2>6. Twoje prawa</h2>
        <p>Masz prawo do:</p>
        <ul>
          <li>dostępu do swoich danych i otrzymania ich kopii;</li>
          <li>sprostowania danych;</li>
          <li>usunięcia danych;</li>
          <li>ograniczenia przetwarzania;</li>
          <li>wniesienia sprzeciwu wobec przetwarzania.</li>
        </ul>
        <p>
          Aby skorzystać z tych praw, napisz na <Placeholder>adres e-mail</Placeholder>. Masz też
          prawo wnieść skargę do Prezesa Urzędu Ochrony Danych Osobowych (ul. Stawki 2, 00-193
          Warszawa, <a href="https://uodo.gov.pl">uodo.gov.pl</a>).
        </p>

        <h2>7. Pliki cookies</h2>
        <p>
          Serwis nie używa obecnie plików cookies. W pamięci przeglądarki (localStorage) zapisujemy
          jedynie Twój wybór dotyczący cookies, aby nie pytać o niego przy każdej wizycie.
        </p>
        <p>
          Jeżeli w przyszłości dodamy narzędzia analityczne, uruchomimy je wyłącznie po wyrażeniu
          przez Ciebie zgody. Zgodę możesz w każdej chwili zmienić przyciskiem „Ustawienia cookies”
          w stopce strony.
        </p>

        <h2>8. Zautomatyzowane przetwarzanie</h2>
        <p>
          Odpowiedzi przygotowuje system wykorzystujący sztuczną inteligencję na podstawie
          wskazanych źródeł. Odpowiedź nie jest decyzją wywołującą wobec Ciebie skutki prawne.
          Zawsze możesz samodzielnie zapoznać się ze źródłami.
        </p>

        <h2>9. Zmiany polityki</h2>
        <p>
          O zmianach tej polityki poinformujemy na tej stronie, podając datę ostatniej aktualizacji.
        </p>
      </article>
    </main>
  );
}
