# Źródła, kwalifikacja i aktualizacja

Audytowalną listą źródeł dopuszczonych do katalogu jest
[`data/sources/catalog.toml`](../data/sources/catalog.toml). W chwili
przeglądu zawiera 32 wpisy: 24 urzędowe, jedno źródło danych publicznych i
7 źródeł fact-checkingowych, z 22 domenami. Są to wpisy katalogu, a nie
potwierdzenie, że wszystkie źródła są dostępne lub zasilone dokumentami.
Oddzielny [katalog interfejsów](../data/sources/api/README.md) opisuje
dostępne i niepotwierdzone API/RSS dla adresów z [links.md](../links.md).

## Zakres i kwalifikacja

Katalog podaje dla każdego wpisu wydawcę, domenę, typ, poziom zaufania,
deklarowane warunki wykorzystania i uzasadnienie kwalifikacji
(`inclusion_rule`). Obejmuje:

- źródła urzędowe i dane pierwotne, m.in. RCB, MSWiA, GIS, MSZ,
  Policję, IMGW i inne właściwe instytucje;
- organizacje fact-checkingowe wyszukiwane przez Google Fact Check Tools API,
  np. Demagog i AFP Sprawdzam, oraz bazę EUvsDisinfo przez MCP.

Kwalifikacja opiera się na roli instytucji lub działalności weryfikacyjnej,
nie na założeniu, że każda jej publikacja jest nieomylna. Wpisy wymagające
potwierdzenia, w tym dotyczące członkostwa organizacji i warunków licencji,
pozostają oznaczone w katalogu. Zgłoszony przez użytkownika URL może również
zostać pobrany do analizy; sama domena ani status źródła nie rozstrzygają
werdyktu. Dla źródła lub usługi niedostępnej należy ujawnić ograniczenie.

## Licencje i zakres wykorzystania

Konkretne deklaracje znajdują się w polu `license` każdego wpisu katalogu:

- dla wielu materiałów instytucji publicznych katalog wskazuje art. 4
  polskiej ustawy o prawie autorskim w odniesieniu do materiałów urzędowych;
  pozostałe treści podlegają warunkom danego serwisu, często oznaczonym
  jako wymagające potwierdzenia. Nie oznacza to automatycznie, że każda
  treść na domenie urzędowej jest wyłączona z ochrony;
- wpis IMGW wskazuje dane publiczne z wymogiem podania źródła;
- dla Google Fact Check katalog wskazuje warunki Google APIs i metadane
  ClaimReview, a dla EUvsDisinfo — konieczność potwierdzenia warunków
  ponownego wykorzystania.

Przy zasilaniu korpusu kolektor gov.pl zapisuje oczyszczony tekst, kolektor
Google Fact Check — metadane recenzji, a EUvsDisinfo — m.in. streszczenie
i analizę. Odrębnie w czasie sprawdzania system może pobierać pełne strony
w celu wyboru dowodów i cytatów. Przed dalszą dystrybucją lub trwałym
archiwizowaniem treści należy osobno zweryfikować właściwe prawa, warunki,
podstawę prawną i retencję; wpis w katalogu nie jest opinią prawną.

## Aktualizacja

Katalog jest synchronizowany z bazą. Jawnie uruchomiony proces `ingest`
pobiera materiały ponownie, rozpoznaje niezmienioną treść po skrócie i zapisuje
zmienioną jako nową wersję. Przebiegi pobierania mają statusy i liczniki,
a data publikacji, jeśli znana, jest oddzielona od daty pobrania. Usunięcie
źródła z katalogu oznacza je jako nieaktywne, nie usuwa automatycznie
historycznych dokumentów. Embeddingi uzupełnia osobny etap `embed`.

Nie należy zakładać, że korpus aktualizuje się sam: wymagane jest
uruchomienie lub zaplanowanie zasilania. Niezależnie od korpusu część źródeł
jest odpytywana na żywo podczas sprawdzania. Dostępność, zakres i licencje
należy okresowo ponownie sprawdzać.

Przykład dla uruchomionego Compose, z katalogu repozytorium:

```bash
docker compose --env-file .env -f deploy/compose.yaml exec api python -m prawdowid.ingestion sync-catalog
docker compose --env-file .env -f deploy/compose.yaml exec api python -m prawdowid.ingestion ingest --all
docker compose --env-file .env -f deploy/compose.yaml exec api python -m prawdowid.ingestion embed --limit 500
docker compose --env-file .env -f deploy/compose.yaml exec api python -m prawdowid.ingestion stats
```

Pobieranie wymaga dostępu do źródeł i odpowiednich kluczy; embeddingi
korzystają z płatnego API Google. Należy przestrzegać warunków źródeł oraz
przed uruchomieniem ustalić dopuszczalny koszt.
