import { CheckForm } from "../components/CheckForm";

export default function Home() {
  return (
    <main id="main" className="container main">
      <nav aria-label="Ścieżka nawigacji" className="breadcrumbs">
        <ol>
          <li>
            <a href="/">Prawdowid</a>
          </li>
          <li aria-current="page">Sprawdź informację</li>
        </ol>
      </nav>

      <div className="layout">
        <article className="content">
          <h1>Sprawdź informację</h1>
          <p className="lead">
            Wklej treść albo link, który budzi Twoje wątpliwości, lub zadaj pytanie. Narzędzie wskaże, co mówią na ten
            temat wiarygodne źródła, i pokaże, na jakiej podstawie udzieliło odpowiedzi.
          </p>
          <CheckForm />
        </article>

        <aside className="sidebar" aria-labelledby="how-it-works">
          <h2 id="how-it-works">Jak to działa</h2>
          <ol className="steps">
            <li>Wklejasz treść lub link albo zadajesz pytanie.</li>
            <li>Narzędzie wyodrębnia twierdzenia, które da się sprawdzić.</li>
            <li>Porównuje je z dokumentami urzędowymi, danymi publicznymi i weryfikacjami fact-checkerów.</li>
            <li>Pokazuje wynik, uzasadnienie i źródła. Gdy dowodów brakuje, mówi o tym wprost.</li>
          </ol>
        </aside>
      </div>
    </main>
  );
}
