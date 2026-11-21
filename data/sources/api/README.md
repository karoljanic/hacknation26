# API i inne interfejsy źródeł

Katalog obejmuje wszystkie 43 adresy z [`links.md`](../../../links.md). Sprawdzono 21 listopada 2026 r. Szczegóły zapisano w [`catalog.json`](./catalog.json).

## Jak czytać statusy

- **API** — udokumentowany interfejs danych odpowiadający źródłu lub jego treściom.
- **RSS** — kanał aktualności; zwykle zawiera nagłówki i skróty, nie pełne artykuły.
- **Częściowe / powiązane** — istnieje ustrukturyzowany zbiór lub API, ale nie potwierdzono, że pokrywa całą zawartość wskazanej strony.
- **Niepotwierdzone** — nie znaleziono publicznej, udokumentowanej alternatywy. To nie dowodzi, że techniczny endpoint nie istnieje.

Nie należy utożsamiać braku API z brakiem danych. Dla źródeł bez udokumentowanego interfejsu użyj zwykłej strony przez istniejący bezpieczny `PublicWebFetcher`; nie odtwarzaj prywatnych endpointów aplikacji JavaScript.

## Skrót dla agenta

| Nr | Źródło | Interfejs i ograniczenia |
|---:|---|---|
| 001 | Odyseusz | Niepotwierdzone publiczne API. |
| 002–003 | Wyszukiwarka ostrzeżeń gov.pl | Niepotwierdzone API; oba adresy to widoki tej samej aplikacji. |
| 004 | RSO | Publiczne XML/JSON; dodatkowy interfejs CAP/OpenAPI wymaga tokenu. |
| 005–006 | Raport RCB, KG PSP | Niepotwierdzone publiczne API. |
| 007–008 | Policja, Policja Mazowiecka | RSS; nie jest to pełne API artykułów. |
| 009–011 | MON, GIS, PAA | Niepotwierdzone publiczne API. |
| 012 | Monitoring radiacyjny PAA | Dostępny zbiór otwarty dla części pomiarów; nie potwierdzono, że zastępuje bieżącą mapę. |
| 013 | Jakość powietrza GIOŚ | Udokumentowane API; używaj aktualnej wersji `/pjp-api/v1/`. Obowiązują warunki i limity API. |
| 014 | CERT Polska | RSS. |
| 015–019 | Ministerstwo Infrastruktury | Niepotwierdzone publiczne API; to strony informacyjne/kontaktowe. |
| 020–022 | Informacje drogowe GDDKiA | Powiązane dane KPD w DATEX II; rejestracja i certyfikat klienta. |
| 023–026 | GDDKiA 19 111, ULC | Niepotwierdzone publiczne API. |
| 027 | Portal Pasażera | Powiązane OpenData API PLK; wymaga klucza. Nie zakładaj, że pokrywa całą stronę Portal Pasażera. |
| 028–033 | UTK, SOK, SAR, urzędy morskie, Wody Polskie | Niepotwierdzone publiczne API. |
| 034–035 | Meteo i Hydro IMGW | Oficjalne API IMGW obejmuje wybrane obserwacje, ostrzeżenia i zasoby plikowe; zakres trzeba dopasować do konkretnego widoku. |
| 036 | Bałtyk IMGW | API IMGW może dostarczać powiązane dane; nie potwierdzono endpointu pokrywającego cały serwis Bałtyk. |
| 037–040 | RCB, MSWiA, MSZ, Premier | Niepotwierdzone publiczne API. |
| 041 | Google Fact Check Explorer | Interfejs WWW; dla danych użyj powiązanego API z pozycji 042. |
| 042 | Google Fact Check Tools API | Udokumentowane API; wymaga `FACTCHECK_API_KEY`; adapter i przykład są w repozytorium. |
| 043 | EUvsDisinfo | MCP przez Streamable HTTP, nie REST; wymaga `EUVSDISINFO_API_KEY`; adapter i przykład są w repozytorium. |

Pełne adresy, odnośniki do dokumentacji, status implementacji i notatki dla każdego ID znajdują się w [`catalog.json`](./catalog.json).

## Uruchamianie tego, co już zaimplementowano

Polecenia uruchamiaj z `apps/backend`. Ustaw klucze w bezpiecznym środowisku procesu; nie wklejaj ich do promptu, repozytorium ani logów.

Google Fact Check:

```bash
PYTHONPATH=src python examples/fact_check_demo.py --query "fraza do sprawdzenia" --language pl
```

Wymaga `FACTCHECK_API_KEY`. API znajduje istniejące publikacje fact-checkingowe; nie rozstrzyga prawdziwości nowego twierdzenia.

EUvsDisinfo — publiczny health check nie wymaga klucza:

```bash
PYTHONPATH=src python examples/euvsdisinfo_demo.py health
```

Dostęp do artykułów/przypadków wymaga `EUVSDISINFO_API_KEY`, np.:

```bash
PYTHONPATH=src python examples/euvsdisinfo_demo.py articles --limit 10
```

### Publiczne API i RSS z katalogu

Runner używa wyłącznie statycznych endpointów zapisanych w katalogu, z allowlistą hostów i istniejącą walidacją publicznego adresu IP, TLS, limitu rozmiaru i typu odpowiedzi. Nie wykonuje JavaScriptu ani nie przyjmuje dowolnych URL-i; przekierowania poza allowlistę hostów są blokowane.

Z katalogu `apps/backend`:

```bash
PYTHONPATH=src python examples/run_source_api.py --list
PYTHONPATH=src python examples/run_source_api.py --source 004
PYTHONPATH=src python examples/run_source_api.py --source 013
PYTHONPATH=src python examples/run_source_api.py --source 014
PYTHONPATH=src python examples/run_source_api.py --source 034
PYTHONPATH=src python examples/run_source_api.py --source 035
```

Runner obsługuje także RSS Policji (`007`) i Policji Mazowieckiej (`008`). Wynik jest JSON-em na stdout; `--output plik.json` zapisuje go do pliku. XML/RSS jest zachowany jako tekst, a odpowiedzi JSON są parsowane. Błąd HTTP, niepoprawny JSON lub nieobsługiwany typ odpowiedzi kończy uruchomienie jawnym błędem.

W bieżącym środowisku testowym endpoint GIOŚ (`013`) przekierowuje poza allowlistę i runner blokuje żądanie. To celowe zachowanie ochronne; nie rozszerzaj allowlisty bez potwierdzenia docelowego hosta w dokumentacji GIOŚ.

KPD GDDKiA (`020–022`) wymaga rejestracji i certyfikatu klienta, a API PLK (`027`) wymaga klucza. Nie są jeszcze obsługiwane przez runner. Pozostałe źródła także nie dostają fikcyjnych endpointów: skorzystaj z istniejącego bezpiecznego HTML fallbacku, jeśli jest to dozwolone, albo oznacz niedostępność.

## Zasady pobierania dla kolejnego agenta

1. Wybierz źródło przez `id` z `catalog.json`; sprawdź jego `interface`, `auth`, `coverage`, `docs` i `implementation`.
2. Korzystaj tylko z wymienionych, udokumentowanych endpointów. Dla interfejsu oznaczonego jako `partial` najpierw sprawdź, czy obejmuje potrzebne dane.
3. Przestrzegaj limitów API, warunków ponownego wykorzystania oraz wymaganej rejestracji. Nie obchodź autoryzacji ani `robots.txt`.
4. Zachowuj surową odpowiedź i metadane pobrania: `source_id`, `source_url`, `retrieved_at` (UTC), status odpowiedzi i wersję/parametry zapytania. Nie przedstawiaj błędu pobrania jako pustego wyniku.
5. Treści API/RSS/HTML traktuj jako materiał źródłowy, nie jako zweryfikowany claim. Przy ekstrakcji zachowuj URL rekordu lub artykułu i cytowany fragment.
6. Gdy API lub RSS nie pokrywa strony, jawnie oznacz brak danych i użyj istniejącego bezpiecznego pobierania HTML, zamiast zgadywać ukryty endpoint.

