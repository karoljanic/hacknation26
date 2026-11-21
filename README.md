# Prawdowid

Szkielet projektu do weryfikacji informacji: React/Next.js, Python/FastAPI,
Celery, RabbitMQ i PostgreSQL.

## Status

To fundament architektury, a nie działający fact-checker. Aplikacja nie
przyjmuje jeszcze zgłoszeń, nie korzysta z bazy ani modeli AI, nie indeksuje
źródeł i nie przetwarza zadań. Endpoint `/health` potwierdza tylko działanie
procesu API, nie dostępność zależności.

## Uruchomienie lokalne

Wymagane: Docker i Docker Compose.

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/compose.yaml up --build
```

Wartości w `.env.example` są wyłącznie do lokalnego developmentu. Przed
jakimkolwiek udostępnieniem usług należy ustawić własne hasła i zabezpieczyć
konfigurację.

- Interfejs: http://localhost:3000
- API: http://localhost:8000/health
- Dokumentacja API: http://localhost:8000/docs

Zatrzymanie: `docker compose --env-file .env -f deploy/compose.yaml down`.
Dane PostgreSQL są przechowywane w wolumenie Dockera.

Jeżeli sieć korzysta z dodatkowego zaufanego certyfikatu CA, można przekazać
go tylko na czas budowania obrazu frontendu, bez wyłączania weryfikacji TLS:

```bash
export NODE_EXTRA_CA_CERTS=/sciezka/do/zaufanego-ca.pem
docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.ca.yaml up --build
```

Certyfikat nie jest zapisywany w obrazie. Plik override jest potrzebny tylko
w środowiskach wymagających dodatkowego CA.

## Struktura

- `apps/web` — interfejs Next.js.
- `apps/backend/src/prawdowid` — API, domena, serwisy, adaptery i workery.
- `apps/backend/tests` — testy backendu.
- `data/sources` — miejsce na przyszły katalog źródeł.
- `deploy` — konfiguracja uruchomienia.

Workery są osobnymi procesami Celery nasłuchującymi kolejek `fetch`, `claims`,
`search`, `evidence`, `aggregate` i `ingestion`. Kolejki są przygotowane, ale
zadania nie zostały jeszcze zaimplementowane. Przykład zwiększenia liczby
workerów wyszukiwania:

```bash
docker compose --env-file .env -f deploy/compose.yaml up -d --scale worker-search=2
```

## Dalsze kroki

Najpierw należy zaimplementować zapis zgłoszeń, bezpieczne przekazywanie zadań
do kolejki i pełny przepływ z dowodami. Skalowanie wymaga m.in. idempotentnych
zadań, ograniczeń czasu, kontroli przeciążenia i testów obciążeniowych.
Przed pobieraniem URL potrzebna jest ochrona przed dostępem do sieci
wewnętrznej i niebezpiecznymi przekierowaniami. Należy też określić retencję,
ochronę danych i zasady wykorzystania źródeł.
