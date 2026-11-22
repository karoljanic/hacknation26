# Prawdowid

Szkielet projektu do weryfikacji informacji: React/Next.js, Python/FastAPI,
Celery, RabbitMQ i PostgreSQL.

## Status

To fundament architektury, a nie działający fact-checker. Dostępne są adaptery
AI, schemat bazy (PostgreSQL + pgvector) i przyjmowanie zgłoszeń z CLI:
tekst/URL/media → maskowanie danych osobowych → ekstrakcja tez → zapis tez
z deduplikacją, oraz zasilanie korpusu źródeł (gov.pl, fact-checki przez
Google Fact Check, EUvsDisinfo) z podziałem na fragmenty i embeddingami.
Endpoint `POST /check` ma walidację wejścia i kontrakt odpowiedzi, ale samo
sprawdzanie informacji zwraca jeszcze `501 Not Implemented`. Nie ma jeszcze
wyszukiwania dowodów ani werdyktów. Endpoint `/health` potwierdza tylko
działanie procesu API; `/health/ready` sprawdza połączenie z bazą i migracje.

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
- API: http://localhost:8001/health
- Dokumentacja API: http://localhost:8001/docs

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

## Lokalna autoryzacja konta X

API obsługuje OAuth 2.0 Authorization Code z PKCE (S256) dla konta
`@prawdowid`. To autoryzacja konta, **nie uruchomiony odbiornik DM ani bot
fact-checkujący**.

1. W X Developer Console skonfiguruj aplikację jako „Web app, automated app
   or bot”. Włącz dostęp do odczytu i wysyłania DM.
2. W prywatnym `.env` ustaw `X_CLIENT_ID`, `X_CLIENT_SECRET` i
   `X_REDIRECT_URI=http://127.0.0.1:8001/auth/x/callback`. Ten sam callback
   wpisz w konsoli X. Jeśli zmieniasz `API_PORT`, zmień port w obu callbackach.
   W `.env.example` klucze muszą pozostać puste.
3. Przebuduj i uruchom API: `docker compose --env-file .env -f deploy/compose.yaml up -d --build api`.
   Zachowaj dodatkowe pliki Compose z CA, jeżeli ich używasz.
4. W profilu przeglądarki zalogowanym jako `@prawdowid` otwórz
   http://127.0.0.1:8001/auth/x i kliknij „Autoryzuj @prawdowid na X”.
   Używaj tego samego hosta co w callbacku, nie zamieniaj `127.0.0.1` na
   `localhost`. Zatwierdź zgodę na X.

Backend sprawdza jednorazowy `state`, powiązanie z ciasteczkiem przeglądarki,
ważność 10 minut, przyznane uprawnienia i nazwę konta przez `/2/users/me`.
Zgoda innego konta nie zastąpi zapisanej autoryzacji. Scopes:
`dm.read dm.write tweet.read users.read offline.access`. Dostęp do X API
jest płatny; odczyt tożsamości konta również może zużyć kredyty.

Token dostępu i refresh token są zapisane atomowo w pliku o uprawnieniach
`0600`, w wolumenie `x_auth_data` (bez szyfrowania na dysku). Nie dodawaj
tokenów do Gita ani nie udostępniaj wolumenu. Poza Dockerem plik znajduje
się w `~/.local/share/prawdowid/x-oauth.json`. `/auth/x/status` pokazuje
wyłącznie nazwę konta i czas wygaśnięcia, nigdy tokeny. Odświeżanie tokenów
i odbieranie/wysyłanie DM będą kolejnym etapem.

API w Compose nasłuchuje tylko na loopback i ma wyłączony access log, aby
nie zapisywać kodów OAuth z callbacka. Nie wystawiaj tych endpointów
publicznie bez dodatkowego uwierzytelnienia. Oczekujące autoryzacje są
przechowywane w pamięci jednego procesu API; po restarcie zacznij nową.

## Limity żądań

`POST /check` wywołuje płatne lub limitowane usługi (modele, API źródeł), więc
każde zgłoszenie przechodzi przez limity (`apps/backend/src/prawdowid/api/rate_limit.py`):

| Limit | Domyślnie | Zmienna |
| --- | --- | --- |
| zgłoszenia jednego klienta na minutę | 20 | `CHECK_RATE_LIMIT_PER_MINUTE` |
| zgłoszenia jednego klienta na dobę | 300 | `CHECK_RATE_LIMIT_PER_DAY` |
| wszystkie zgłoszenia na dobę (twardy sufit kosztów) | 5000 | `CHECK_GLOBAL_LIMIT_PER_DAY` |
| równocześnie przetwarzane zgłoszenia | 8 | `CHECK_MAX_CONCURRENT` |

Przekroczenie daje `429` (limit klienta) lub `503` (limit globalny lub
przeciążenie) z nagłówkiem `Retry-After` — nigdy werdykt. Odrzucone żądanie nie
zużywa limitu. Strona pokazuje to jako osobny komunikat. Przy testach
obciążeniowych lub jury podnieś limity w `.env`.

Ograniczenia: liczniki są w pamięci jednego procesu API (restart je zeruje), a
klientem jest adres połączenia. Za serwerem pośredniczącym, który nadpisuje
`X-Forwarded-For`, ustaw `CHECK_TRUST_FORWARDED_FOR=true` (używany jest ostatni
wpis); bez tego wszyscy użytkownicy interfejsu WWW dzielą jeden limit klienta.
Nie włączaj tej zmiennej, gdy API lub strona są dostępne bezpośrednio —
nagłówek może wtedy podszyć dowolny klient.

## Ochrona przed prompt injection

Każde pole tekstowe wpisane przez użytkownika (treść, pytanie oraz pytanie do
linku lub pliku) jest sprawdzane pod kątem prompt injection i jailbreaków,
zanim cokolwiek zostanie uruchomione
(`apps/backend/src/prawdowid/security/injection_guard.py`). Dwa detektory
działają równolegle, a zgłoszenie jest odrzucane (`422`, komunikat po polsku),
gdy którykolwiek osiągnie swój próg. Nie powstaje wtedy przebieg i nie są
wywoływane modele ani źródła. W logu zapisywane są wyniki i typ zgłoszenia,
nigdy sam tekst.

| Detektor | Jak działa | Próg | Zmienna |
| --- | --- | --- | --- |
| JEV (TypeSafe) | jedno stałe pytanie tak/nie; tekst użytkownika trafia wyłącznie do danych (`state`), nigdy do instrukcji | 0.5 | `INJECTION_JEV_THRESHOLD` |
| Guardrails `DetectJailbreak` | lokalnie na CPU: podobieństwo do znanych jailbreaków, wykrywanie „prompt saturation”, klasyfikator RoBERTa | 0.81 | `INJECTION_GUARDRAILS_THRESHOLD` |

- Wynik testu na 24 polskich i angielskich przykładach oraz prawdziwym
  jailbreaku z odgrywaniem ról: JEV dawał zwykłym zgłoszeniom ≤ 0,05, atakom
  ≥ 0,84. Guardrails przy 0,81 nie blokował żadnego zwykłego zgłoszenia i
  łapał klasyczne angielskie jailbreaki (DAN, odgrywanie ról), ale polskie
  ataki ocenia tak samo jak zwykły tekst — po polsku chroni JEV.
- JEV działa, gdy ustawiony jest `TYPESAFE_API_KEY` (bez klucza działa tylko
  Guardrails, z ostrzeżeniem w logu). Dodaje ok. 0,2 s i jedno wywołanie
  TypeSafe na pole.
- Fail closed: gdy detektora nie da się załadować albo wywołanie się nie uda,
  `/check` zwraca `tech_failure` i nie przetwarza zgłoszenia.
- Guardrails: telemetria i zdalne wnioskowanie są wyłączone w kodzie, a modele
  ładują się offline (`HF_HUB_OFFLINE=1`), więc tekst nie opuszcza serwera
  tą drogą. Obraz instaluje PyTorch w wersji CPU.
- To jedna warstwa, nie gwarancja: treści pobierane z linków, odczytane z
  plików i strony z dowodami nie są nią sprawdzane.

Modele Guardrails są pobierane przy budowaniu obrazu z ustalonych rewizji
(`apps/backend/scripts/fetch_guardrails_models.py`, tylko pliki safetensors i
tokenizery). Lokalnie, poza Dockerem:

```bash
cd apps/backend
HF_HOME=~/.cache/prawdowid/hf python scripts/fetch_guardrails_models.py
export HF_HOME=~/.cache/prawdowid/hf HF_HUB_OFFLINE=1
```

### Komponenty zewnętrzne i licencje

- [guardrails-ai](https://github.com/guardrails-ai/guardrails) (Apache-2.0) i
  [guardrails-ai-detect-jailbreak](https://github.com/guardrails-ai/guardrails-hub-monorepo/tree/main/detect_jailbreak/py) (MIT).
- Modele: `zhx123/ftrobertallm` (MIT), `sentence-transformers/all-MiniLM-L6-v2`
  i `google-bert/bert-base-cased` (Apache-2.0),
  `GuardrailsAI/prompt-saturation-attack-detector` (strona modelu nie podaje
  licencji — do potwierdzenia przed dalszą dystrybucją obrazu).

## Deduplikacja tez

Powtórzone twierdzenie jest łączone z już zapisaną tezą: najpierw po dokładnym
tekście, potem po podobieństwie embeddingów (Gemini, 768 wymiarów, próg
kosinusowy 0,12 do kalibracji). Podobieństwo wektorowe nie wystarcza: tezy
różniące się liczbą („VAT 23%” / „VAT 25%”) albo zaprzeczeniem leżą blisko
siebie, a ocena jednej nie jest oceną drugiej. Dlatego łączenie wymaga też
tych samych liczb i tej samej polaryzacji (`same_specifics`). Awaria lub limit
embeddingów nie przerywa sprawdzenia — wtedy działa dopasowanie po dokładnym
tekście, co odnotowuje `verification_run.models`.

## Baza danych

PostgreSQL 17 z rozszerzeniem pgvector (`pgvector/pgvector:0.8.1-pg17`).
Schemat: `apps/backend/src/prawdowid/persistence/models.py`, migracje Alembic
w `persistence/migrations`. W Compose usługa `migrate` wykonuje
`alembic upgrade head` przed startem API i workerów.

Graf dowodowy jest zapisany relacyjnie: węzły to tabele (`source`,
`document`, `passage`, `claim`, `submission`, …), a krawędzie to tabele
z atrybutami: `evidence` (fragment → teza: supports/refutes/adds_context, cytat),
`claim_relation` (teza → teza: same_as/contradicts/supersedes/…),
`document_relation` i `submission_claim` (zgłoszenie → teza: states/implies/asks).
Werdykt (`assessment`) jest oddzielony od statusu przetwarzania
(`verification_run.status`: ok/tech_failure/needs_clarification/out_of_scope).

Do pracy lokalnej z backendem poza kontenerem `deploy/compose.dev.yaml`
publikuje porty bazy i RabbitMQ na localhost:

```bash
docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.dev.yaml up -d postgres rabbitmq
export DATABASE_URL=postgresql+psycopg://prawdowid:change-me-local@127.0.0.1:5432/prawdowid
cd apps/backend && alembic upgrade head
```

### Zapis zgłoszeń i tez

`ClaimIntake` (`services/intake.py`) łączy przygotowanie wejścia i
`ClaimExtractor` z bazą:

1. tworzy `submission` (z datą wygaśnięcia, domyślnie 30 dni) i
   `verification_run`;
2. maskuje oczywiste dane osobowe (e-mail, telefon, PESEL, nr konta/karty)
   **przed** wysłaniem tekstu do modelu; dla URL zapisuje tylko link, nie treść
   artykułu;
3. wyodrębnia tezy, a każdą sprawdza z bazą: najpierw dokładne dopasowanie
   (po normalizacji wielkości liter, spacji i końcowej interpunkcji), potem
   podobieństwo wektorowe (odległość kosinusowa ≤ 0,12, ten sam rodzaj tezy
   i model embeddingu; próg wymaga kalibracji). Dopasowana teza jest
   podłączana do zgłoszenia zamiast tworzenia duplikatu; sposób dopasowania
   zapisuje się w `submission_claim.match`;
4. zapisuje status, czas i użyte modele/tokeny w `verification_run`. Błąd
   dostawcy, limit lub brak klucza daje status `tech_failure`, a brak tez lub
   nieczytelne wejście — `needs_clarification`; żaden z nich nie jest werdyktem.

```bash
python examples/extract_claims.py --store --text "Rząd podniósł VAT do 25%."
python examples/extract_claims.py --store --url "https://example.org/artykul" --channel web
```

Testy `tests/test_intake.py` używają atrap modelu i embeddingu, ale
prawdziwej bazy z `DATABASE_URL`; bez dostępnej bazy są pomijane.

W `POST /check` przebieg zamyka dopiero `VerificationPipeline`, po ocenie
wszystkich tez (do tego czasu ma status `running`). Zapisuje wtedy w
`verification_run`:

- status i czas całego sprawdzenia (`latency_ms`, od przyjęcia do werdyktu);
- zużycie każdego wywołania modelu w `models`: odczyt mediów, ekstrakcja,
  embedding, decyzje Jev (wybór działów gov.pl i linków do pobrania) i ocena,
  łącznie z ponowieniami;
- szacowany koszt w `est_cost_usd`, gdy ustawiono ceny modeli
  (`MODEL_PRICES_USD_PER_MTOK`, np. `{"gemini-3.5-flash": [0.3, 2.5], "jev-1.14.0": [0.1, 0.4]}`
  w USD za milion tokenów wejścia i wyjścia; nazwy modeli tak, jak zapisuje je
  `models`, np. Jev podaje wersję zamiast `jev-latest`). Gdy któryś użyty model
  z tokenami nie ma ceny, koszt zostaje pusty zamiast zaniżonego;
- wykryte próby prompt injection w `injection_flags` (adres źródła i dosłowny
  fragment). Nie zmieniają one werdyktu. Ukryty tekst stron (CSS, `aria-hidden`,
  komentarze HTML) jest usuwany, zanim trafi do modelu.

Wymagany jest tylko `GEMINI_API_KEY`. Brak klucza lub awaria Google Fact Check,
EUvsDisinfo albo TypeSafe (Jev) nie blokuje sprawdzenia: odpowiedź podaje
niedostępne źródło w ograniczeniach danej tezy.

## Struktura

- `apps/web` — interfejs Next.js.
- `apps/backend/src/prawdowid` — API, domena, serwisy, adaptery i workery.
- `apps/backend/src/prawdowid/ai/adapters` — osobne kontrakty LLM, decyzji i embeddingów.
- `apps/backend/src/prawdowid/ai/instances` — implementacje Gemini i Jev.
- `apps/backend/tests` — testy backendu.
- `data/sources/catalog.toml` — katalog źródeł do zasilania korpusu.
- `data/sources/api` — statusy API/RSS dla adresów z `links.md` i instrukcje uruchomienia.
- `deploy` — konfiguracja uruchomienia.

### Workery

Workery Celery nasłuchują osobnych kolejek. Zaimplementowane są zadania
zasilania korpusu (`ingestion`) i tworzenia embeddingów (`embedding`);
`fetch`, `claims`, `search`, `evidence` i `aggregate` nie mają jeszcze zadań.
Szczegóły uruchamiania API i RSS z listy źródeł są w [katalogu API](data/sources/api/README.md).

Przykład zwiększenia liczby workerów wyszukiwania:

```bash
docker compose --env-file .env -f deploy/compose.yaml up -d --scale worker-search=2
```

## Adaptery AI

Trzy osobne kontrakty w `apps/backend/src/prawdowid/ai/adapters` oddzielają aplikację od
dostawców: `llm.py` (`LLMAdapter`), `decision.py` (`DecisionAdapter`) oraz
`embedding.py` (`EmbeddingAdapter`). Wspólny typ `Media` jest w `media.py`.
Implementacje znajdują się wyłącznie w `apps/backend/src/prawdowid/ai/instances`;
kontrakty nie importują SDK dostawców. Konkretny model wybiera
się przy tworzeniu implementacji, a zamiana dostawcy wymaga implementacji
odpowiedniego kontraktu.

- `GeminiAdapter` implementuje `LLMAdapter` i generuje tekst za pomocą Google
  Gen AI SDK. Flaga
  `search=True` udostępnia modelowi Google Search; wynik zawiera również
  zwrócone przez dostawcę metadane grounding, jeśli są dostępne. Włączenie
  narzędzia nie gwarantuje, że model go użyje.
- `JevAdapter` implementuje `DecisionAdapter` i korzysta z TypeSafe System One.
  W jednym wywołaniu może ocenić
  kilka nazwanych pytań: **Noul** (prawdopodobieństwo odpowiedzi „tak”),
  **Choice** (wybór i rozkład prawdopodobieństw), **Score** (ocena na
  uporządkowanej skali) oraz **Box** (położenie obiektu na obrazie).
  Pytania i wyniki są typami projektu, niezależnymi od SDK dostawcy. Obsługa
  obrazów i Box jest eksperymentalna po stronie dostawcy; podanie obrazu jawnie
  włącza ten tryb.
- `GeminiEmbeddingAdapter` implementuje `EmbeddingAdapter` i używa domyślnie
  `gemini-embedding-2`. Przyjmuje tekst albo bajty materiału z typem MIME.
  Model obsługuje m.in. obrazy, audio, wideo i PDF, z limitami i formatami
  określonymi przez Google. API zwraca wektor i nazwę wybranego modelu.
  Porównuj tylko wektory zgodne modelem, wymiarem i sposobem przygotowania;
  zmiana modelu lub wymiaru wymaga ponownego zindeksowania.

Modele i ich pewność nie stanowią dowodu prawdziwości. Metadane wyszukiwania
nie są zweryfikowanymi cytatami: źródła i ich dopasowanie do tez wymagają
niezależnej kontroli. Błędy dostawców są przekazywane wywołującemu.

### Przykłady

Ustaw `GEMINI_API_KEY` lub `TYPESAFE_API_KEY` w środowisku, zależnie od
wybranego przykładu. Klucze dla kontenerów można również wpisać do lokalnego,
ignorowanego przez Git pliku `.env`; Compose przekazuje je workerom.

```bash
cd apps/backend
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'

python examples/ai_demo.py fast       # Gemini 2.5 Flash
python examples/ai_demo.py search     # Gemini 3.8 Flash + Google Search
python examples/ai_demo.py search --model gemini-2.5-flash  # Alternatywny model wyszukiwania
python examples/ai_demo.py decision   # Jev: Noul, Choice, Score
python examples/ai_demo.py image --image /sciezka/obraz.png  # Jev: Box
python examples/ai_demo.py embed-text  # Gemini: embedding tekstu
python examples/ai_demo.py embed-image --image /sciezka/obraz.png
```

W każdym przykładzie model można zmienić przez `--model NAZWA`. Przykłady
wykonują rzeczywiste, potencjalnie płatne żądania dopiero po ich uruchomieniu.
Obraz jest wysyłany do odpowiedniego dostawcy: TypeSafe dla `image`, Google
dla `embed-image`. Tekst także jest wysyłany do odpowiedniego dostawcy. Nie
używaj danych wrażliwych bez odpowiednich zgód i oceny zasad
przetwarzania, retencji oraz miejsca przetwarzania danych przez dostawcę.
Dostęp do wybranego modelu i Google Search zależy od konta, limitów i aktualnej
oferty dostawcy. `jev-latest` jest ruchomym aliasem; do powtarzalnych testów
można wskazać dostępną wersję modelu.

HTTP 429 oznacza limit dostawcy lub konta, a nie poprawną odpowiedź modelu.
W razie limitu dla domyślnego modelu wyszukującego można jawnie wybrać inny
przez `--model`; aplikacja nie zmienia modelu automatycznie.

Jeśli Python/Jev zgłasza `CERTIFICATE_VERIFY_FAILED` w sieci z dodatkowym CA,
ustaw `SSL_CERT_FILE` na zaufany zestaw zawierający certyfikaty systemowe
oraz dodatkowy CA, np.:

```bash
SSL_CERT_FILE=/sciezka/pelny-ca-bundle.pem python examples/ai_demo.py decision
```

`NODE_EXTRA_CA_CERTS` dotyczy Node.js, nie SDK Pythona. W kontenerze trzeba
również zamontować zestaw CA i przekazać `SSL_CERT_FILE` ze ścieżką wewnątrz
kontenera. Nie wyłączaj weryfikacji TLS. Istniejący override
`deploy/compose.ca.yaml` konfiguruje wyłącznie budowanie frontendu.

Przykład można też uruchomić w kontenerze po zbudowaniu usług:

```bash
docker compose --env-file .env -f deploy/compose.yaml run --rm --no-deps worker-claims python examples/ai_demo.py decision
```

Testy backendu nie wykonują zewnętrznych żądań: `pytest apps/backend/tests`.
Dokumentacja dostawców: [Google Search grounding](https://ai.google.dev/gemini-api/docs/generate-content/google-search),
[Google embeddings](https://ai.google.dev/gemini-api/docs/embeddings),
[TypeSafe System One](https://docs.typesafe.ai/concepts/system-one) oraz
[TypeSafe Models](https://docs.typesafe.ai/models).

## Ekstrakcja claims

Pierwszy etap działa dla tekstu, pytań, URL artykułu oraz lokalnych plików obrazu,
audio i wideo. Wspólny `InputSegment`
przechowuje pochodzenie (tekst, pytanie, artykuł, OCR, transkrypcja lub klatka),
opcjonalny URL, nazwę materiału, czasy fragmentu nagrania oraz przybliżony obszar
tekstu na obrazie lub klatce.

Z aktywnym środowiskiem backendu i ustawionym `GEMINI_API_KEY`:

```bash
python examples/extract_claims.py --text "Nowe przepisy obowiązują od jutra."
python examples/extract_claims.py --question "Dlaczego Polska głosowała przeciw?"
python examples/extract_claims.py --url "https://example.org/artykul"
python examples/extract_claims.py --image /sciezka/zrzut.png
python examples/extract_claims.py --audio /sciezka/nagranie.wav
python examples/extract_claims.py --video /sciezka/film.mp4
python examples/extract_claims.py --segments /sciezka/fragmenty.json
```

`InputPreparer` zamienia każde wejście na wspólne fragmenty:

- URL: `PublicWebFetcher` pobiera HTML, a parser wydziela nagłówki i akapity
  artykułu bez nawigacji, skryptów i stopek. Zachowuje końcowy URL, tytuł
  oraz czas pobrania (nie datę publikacji). Pobieranie dopuszcza wyłącznie
  publiczne adresy HTTP(S), porty 80/443, bez danych logowania. Każde
  przekierowanie przechodzi ponowną kontrolę adresów; połączenie jest przypięte
  do sprawdzonego IP, a TLS weryfikuje oryginalną nazwę hosta. Limity:
  3 przekierowania, 2 MB HTML i 60 tys. znaków treści, bez cichego obcinania.
  Budżet operacji sieciowych wynosi 10 s; systemowe DNS i powolne nagłówki
  wymagają dodatkowego twardego limitu czasu na poziomie przyszłego workera.
- Obraz: multimodalny LLM odczytuje tekst i przybliżone obszary w skali `[0,1]`.
- Audio: multimodalny LLM transkrybuje mowę w oryginalnym języku ze znacznikami
  czasu w sekundach.
- Wideo: model odczytuje mowę i widoczny tekst jako osobne fragmenty
  `audio_transcript` i `video_frame`, ze znacznikami czasu.

Odczyt mediów używa `LLMAdapter.generate_structured(..., media=Media(...))`,
bez zależności serwisu od SDK Google. `--preparation-model` wybiera model
odczytu mediów, a `--model` model ekstrakcji claims. Domyślnie oba to
`gemini-2.5-flash`. Wynik CLI rozdziela metadane i tokeny przygotowania danych
od ekstrakcji claims; zawiera także uwagi modelu dotyczące odczytu.

To odczyt multimodalnym LLM, nie deterministyczny silnik OCR/ASR: tekst,
prostokąty i czasy mogą zawierać błędy i wymagają oceny na rzeczywistych mediach.
Walidacja sprawdza strukturę i spójność pochodzenia, nie porównuje transkrypcji
z nagraniem. Cisza, brak tekstu, nieczytelne media i puste odpowiedzi zgłaszają
jawne błędy, a nie werdykt ani pusty sukces.

Lokalny limit pliku to **12 MB** (pozostawia miejsce na base64 i schemat
w żądaniu inline). CLI obsługuje JPG/PNG/WebP, WAV/MP3/AAC/OGG/FLAC i
MP4/WebM/MOV; polecane są krótkie klipy. Większe materiały wymagają osobnego
uploadu lub podziału na fragmenty, których jeszcze nie zaimplementowano.
Nie są obsługiwane strony wymagające JavaScriptu, logowania, paywalla ani
bezpośrednie pobieranie nagrań z TikToka/Instagrama. Odpowiedzi skompresowane
są odrzucane; fetcher prosi o nieskompresowany HTML.

Przykładowy plik fragmentów:

```json
[
  {
    "id": "segment-1",
    "text": "Nowe przepisy obowiązują od jutra.",
    "origin": "audio_transcript",
    "start_seconds": 12.5,
    "end_seconds": 16.0
  }
]
```

`ClaimExtractor` korzysta z `LLMAdapter.generate_structured`, wymusza schemat
odpowiedzi i sprawdza unikalność identyfikatorów, istnienie fragmentów oraz
dokładne cytaty. Błędne odpowiedzi powodują wyjątek, nie pustą listę ani werdykt.
Wynik zawiera tezy, pytania lub opinie, referencje, niejasności oraz metadane
modelu i tokenów. Cytat w wejściu dowodzi pochodzenia tezy, nie jej prawdziwości.
Rozbicie na tezy, ich kompletność i poprawność interpretacji wymagają jeszcze
oceny na rzeczywistych przykładach. Nie zaimplementowano jeszcze podłączenia
ekstraktora do API, kolejki ani wyszukiwania dowodów. Wywołanie przykładu wysyła
całą treść fragmentów do Google i może być płatne. W przypadku obrazu, audio
lub wideo cały plik jest wcześniej wysyłany do Google do odczytu. Nie wysyłaj
danych wrażliwych bez odpowiednich zgód.

## Źródło Google Fact Check

`GoogleFactCheckAdapter` w `prawdowid/adapters/sources/google_fact_check.py`
implementuje niezależny kontrakt `FactCheckSource`. Wyszukuje istniejące
fact-checki; nie tworzy recenzji i nie wydaje własnego werdyktu.

Włącz **Fact Check Tools API** w projekcie Google Cloud i ustaw
`FACTCHECK_API_KEY` w środowisku. W `.env.example` jest tylko puste pole;
rzeczywisty klucz zapisuj wyłącznie lokalnie, nigdy w pliku przykładowym.
Compose przekazuje zmienną workerom, ale nie frontendowi. Przykłady Python
nie wczytują `.env` automatycznie: poza Compose trzeba przekazać zmienne
środowiskowe samodzielnie.

```bash
cd apps/backend
pip install -e '.[test]'

python examples/fact_check_demo.py --query "Ziemia jest płaska" --language pl
python examples/fact_check_demo.py --publisher demagog.org.pl --page-size 5
python examples/fact_check_demo.py --image-url "https://example.org/obraz.jpg"
python examples/fact_check_demo.py --query "szczepionki" --language pl --max-age-days 30
```

Wyszukiwanie tekstowe obsługuje zapytanie, język, domenę wydawcy, wiek
wyników i paginację. Wyszukiwanie obrazowe obsługuje **publiczny URL obrazu**,
język i paginację. Google nie przyjmuje tutaj lokalnego pliku ani bajtów obrazu;
adapter nie publikuje prywatnych plików automatycznie. Dostępność obrazu dla
serwerów Google nie jest gwarantowana.

`--page-token TOKEN` pobiera następną stronę; pozostałe parametry muszą
pozostać takie same. Alternatywą jest `--offset N`, bez tokenu. Lokalny limit
strony to 1–100 wyników. Brak filtra języka oznacza wyszukiwanie bez tego
ograniczenia.

Połączenie z istniejącym przygotowaniem wejścia i ekstrakcją claims:

```bash
python examples/extract_claims.py --text "Przykładowa teza do sprawdzenia." --fact-check
python examples/extract_claims.py --url "https://example.org/artykul" --fact-check
python examples/extract_claims.py --image /sciezka/obraz.png --fact-check
python examples/extract_claims.py --audio /sciezka/nagranie.wav --fact-check
python examples/extract_claims.py --video /sciezka/film.mp4 --fact-check
```

Te warianty wymagają również `GEMINI_API_KEY`: obraz przechodzi OCR, a audio
i wideo odczyt/transkrypcję, następnie wyodrębnione tezy są wyszukiwane
**tekstowo** w Fact Check. Nie jest to natywne wyszukiwanie audio/wideo ani
porównywanie lokalnego obrazu z bazą obrazów.

`FactCheckTool.search_claims` wiąże wyniki z identyfikatorami tez, pomija
opinie (zwraca ich identyfikatory) i współdzieli jedno żądanie dla identycznych
zapytań w danej paczce. Domyślnie wyszukuje pierwszą stronę po 5 wyników
w języku polskim, maksymalnie 10 różnych zapytań. Przekroczenie limitu
powoduje błąd przed wysłaniem żądań, nie ciche pominięcie tez.

Flagi CLI `--fact-check` i `--store` są rozłączne: wybierz wyszukiwanie
fact-checków albo zapis zgłoszenia do bazy.

Wyniki zachowują autora tezy, daty, wydawcę, URL, tytuł i oryginalną ocenę
`textual_rating`, a także czas pobrania i token kolejnej strony. To kandydaci
do dalszej analizy: podobna teza może mieć inną datę lub kontekst. Pusta lista
oznacza brak trafień, **nie fałsz**. API zwraca metadane recenzji, nie pełny
tekst artykułu ani zweryfikowane fragmenty dowodowe.

Klucz trafia do nagłówka, nie URL. Adapter nie podąża za przekierowaniami
i nie ponawia automatycznie żądań; błędy HTTP (w tym limity 429), sieciowe
i błędne odpowiedzi są jawne. Limit odpowiedzi wynosi 2 MB, timeout operacji
HTTP 10 s. Każde żądanie zużywa limit Google, a przygotowanie mediów/claims
może naliczać koszt LLM. Ta integracja jest dostępna przez serwis i CLI;
nie dodaje jeszcze zadań Celery ani endpointu API.

Dokumentacja: [claims.search](https://developers.google.com/fact-check/tools/api/reference/rest/v1alpha1/claims/search),
[claims.imageSearch](https://developers.google.com/fact-check/tools/api/reference/rest/v1alpha1/claims/imageSearch).

## Źródło EUvsDisinfo MCP

`EUvsDisinfoAdapter` w `prawdowid/adapters/sources/euvsdisinfo.py` korzysta
z oficjalnego Python MCP SDK i endpointu `https://mcp.euvsd.info/mcp`.
Obsługuje wszystkie cztery narzędzia udokumentowane przez dostawcę:

- `cases`: przypadki dezinformacji, `offset >= 0`, `limit` od 1 do 100;
- `articles`: artykuły analityczne, takie same parametry paginacji;
- `search`: wyszukiwanie pełnotekstowe artykułów i przypadków, zapytanie
  z co najmniej 3 znakami, opcjonalne `date_from` i `date_to`;
- `narratives`: narracje, opcjonalnie z zakresem dat.

Zakresy dat są walidowane lokalnie i wysyłane jako `YYYY-MM-DD`.
`date_from > date_to` jest błędem. Paginacja pobiera jedną wskazaną stronę,
nie cały katalog automatycznie. Do następnej strony dodaj `limit` do `offset`.

Ustaw osobny `EUVSDISINFO_API_KEY` w środowisku lub lokalnym `.env` dla Compose.
Klucz Google/TypeSafe nie zastępuje klucza EUvsDisinfo. Domyślnie klucz trafia
do `Authorization: Bearer`, nie URL. Przykłady:

```bash
cd apps/backend
pip install -e '.[test]'

python examples/euvsdisinfo_demo.py health
python examples/euvsdisinfo_demo.py tools
python examples/euvsdisinfo_demo.py cases --limit 10 --offset 0
python examples/euvsdisinfo_demo.py articles --limit 5 --offset 5
python examples/euvsdisinfo_demo.py search --query "Mariupol"
python examples/euvsdisinfo_demo.py search --query "biolab" --date-from 2024-01-01 --date-to 2024-12-31
python examples/euvsdisinfo_demo.py narratives --date-from 2025-01-01 --date-to 2025-03-31
```

`health` wywołuje publiczny endpoint `/health` i nie wymaga klucza.
`tools` zwraca opisy i schematy oraz ewentualny `next_cursor`; kolejną stronę
katalogu pobierzesz przez `tools --cursor TOKEN`. Zwrócone nazwy i opisy nie
są automatycznie wykonywane: adapter udostępnia tylko cztery powyższe operacje.

Domyślny tryb `stateful` wykonuje handshake `initialize`, SDK wysyła
powiadomienie initialized, obsługuje identyfikator sesji oraz JSON/SSE
w Streamable HTTP, a przy zamknięciu próbuje zakończyć sesję przez DELETE.
Tryb `stateless` nie wykonuje handshake: przekazuje wygenerowany po stronie
klienta identyfikator sesji, a serwer obsługuje żądania jako efemeryczne.
Nie należy polegać w nim na korelacji ani wznawianiu zdarzeń pomiędzy żądaniami.

```bash
python examples/euvsdisinfo_demo.py --mode stateless cases --limit 10
python examples/euvsdisinfo_demo.py --auth x-api-key search --query "biolab"
python examples/euvsdisinfo_demo.py --auth query articles --limit 5
```

Obsługiwane są trzy sposoby autoryzacji: `bearer`, `x-api-key` i `query`.
Ostatni jest dostępny wyłącznie jako jawna opcja kompatybilności: klucz w URL
może znaleźć się w logach HTTP/proxy. Preferuj nagłówki i nie publikuj logów
z kluczami. W sieci z dodatkowym CA obowiązuje opisane wyżej `SSL_CERT_FILE`;
weryfikacja TLS nie jest wyłączana.

Użycie z kodu aplikacji:

```python
import os

from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
from prawdowid.domain.euvsdisinfo import EUvsDisinfoConnection, EUvsDisinfoSearchRequest

async def search_euvsdisinfo():
    connection = EUvsDisinfoConnection(api_key=os.environ["EUVSDISINFO_API_KEY"])
    async with EUvsDisinfoAdapter.connect(connection) as source:
        return await source.search(EUvsDisinfoSearchRequest(query="Mariupol"))
```

Wynik zachowuje bloki MCP `content`, opcjonalne `structured_content` oraz
czas pobrania. Nie zakładamy nieudokumentowanego opakowania tablic wyników:
jeśli serwer zwraca JSON wewnątrz tekstowego bloku MCP, tekst jest zachowany
bez zmian. Dotyczy to też oryginalnych pól rekordów (`claim`, `fact`, daty,
odnośniki, kraje, słowa kluczowe, języki i miniatury).

Błędy JSON-RPC zachowują kod, `isError` z narzędzia powoduje wyjątek, a błędy
transportu nie są zamieniane na puste wyniki. Timeout HTTP wynosi 10 s
(odczyt 30 s), oczekiwanie na odpowiedź MCP 30 s. Zserializowany wynik narzędzia
jest ograniczony do 2 MB po odebraniu/dekodowaniu przez SDK; nie jest to
limit bajtów pobieranych przez transport.

Dane EUvsDisinfo są materiałem źródłowym, nie automatycznym werdyktem.
Adapter nie interpretuje zdalnych opisów jako instrukcji i nie wywołuje LLM
ani innych narzędzi. Jest dostępny z kodu i CLI; nie dodaje jeszcze indeksu,
autonomicznego routingu LLM ani zadań Celery.

## Korpus źródeł

Katalog źródeł do audytu: [`data/sources/catalog.toml`](data/sources/catalog.toml).
Każdy wpis podaje rodzaj źródła (`official`, `public_data`, `fact_checker`,
`media`), domenę, poziom zaufania (`tier`), licencję, **zasadę kwalifikacji**,
opcjonalne neutralne ostrzeżenie o statusie źródła oraz kolektor z parametrami.
Fakty oznaczone „do potwierdzenia” (członkostwo IFCN/EFCSN, warunki licencji)
wymagają sprawdzenia przy przeglądzie katalogu.

Zasady:

- **kwalifikacja** — tylko źródła urzędowe/pierwotne (tier 1) oraz organizacje
  i bazy weryfikujące informacje (tier 2); uzasadnienie w `inclusion_rule`;
- **aktualizacja** — każde pobranie zapisuje przebieg w `ingestion_run`
  (status, liczniki, błędy, parametry). Dokument jest identyfikowany przez
  (źródło, URL, skrót treści): niezmieniona strona nie jest zapisywana ponownie,
  zmieniona treść tworzy nową wersję, a stara zostaje do audytu;
- **wykluczenie** — usunięcie wpisu z katalogu oznacza źródło jako nieaktywne
  (dokumenty pozostają, nowe nie są pobierane);
- data publikacji (`published_at`, jeśli źródło ją podaje) i data pobrania
  (`fetched_at`) są zapisywane osobno; brak daty publikacji jest jawny (NULL);
- status źródła (np. sankcje UE) nigdy sam nie rozstrzyga o prawdziwości.

Przetwarzanie składa się z niezależnych etapów, które komunikują się wyłącznie
przez bazę (żaden nie wywołuje kolejnego):

1. `sync-catalog` — katalog → tabela `source`;
2. `ingest` — kolektor → `document` + `passage` (fragmenty ~1000 znaków według
   akapitów, z dokładnymi przesunięciami `char_start`/`char_end`);
3. `embed` — uzupełnia brakujące wektory fragmentów i tez (`embedding IS NULL`),
   niezależnie od tego, skąd pochodzą. Błąd dostawcy zostawia NULL do kolejnego
   uruchomienia; równoległe workery nie przetwarzają tych samych wierszy.

| Kolektor | Źródło | Klucz | Co zapisujemy |
| --- | --- | --- | --- |
| `gov_pl` | strony i listy aktualności gov.pl (+ PDF-y) | – | oczyszczony tekst strony (bez skryptów, komentarzy i ukrytych elementów) |
| `google_fact_check` | recenzje ClaimReview wybranego wydawcy | `FACTCHECK_API_KEY` | teza, autor tezy, data, **oryginalna** ocena wydawcy, link — bez treści artykułu |
| `euvsdisinfo` | przypadki i artykuły EUvsDisinfo (MCP) | `EUVSDISINFO_API_KEY` | streszczenie tezy, analiza, słowa kluczowe, kraje, język artykułu, link |

Ocena fact-checkera jest przechowywana dosłownie w `document.metadata`
(`textual_rating`) i nie jest tu zamieniana na werdykt. Brak klucza API daje
przebieg ze statusem `skipped`, awaria źródła — `failed`; żaden z nich nie jest
traktowany jako „brak dokumentów”.

### Źródła czytane na żywo przy sprawdzaniu

Przy każdej tezie model decyzyjny (Jev) wybiera z katalogu maksymalnie 3
źródła oficjalne, które realnie dotyczą tematu, i czyta ich bieżącą treść:

| Kolektor | Źródła | Jak czytane |
| --- | --- | --- |
| `gov_pl` | sekcje gov.pl (RCB, MSWiA, MON, GIS, PAA, Infrastruktura, KG PSP, Wody Polskie, GDDKiA, KPRM) | parser gov.pl, najnowsze artykuły z list |
| `official_page` | Policja (KGP, mazowiecka), GDDKiA – utrudnienia, ULC, UTK, Portal Pasażera, SOK, SAR, urzędy morskie w Gdyni i Szczecinie, GIOŚ, CERT Polska | strona pobierana tylko z domeny z katalogu, bez ukrytego tekstu |
| `imgw` | IMGW-PIB: ostrzeżenia meteo i hydro, pomiary synop i hydro | otwarte API |

Lista aktualności czy zrzut API to dziesiątki tysięcy znaków o innych sprawach,
więc do oceny trafiają tylko dokumenty i fragmenty mające najwięcej słów
wspólnych z tezą i jej frazami wyszukiwania (`services/relevance.py`).
Niezależnie od tego zawsze odpytywane są Google Fact Check, EUvsDisinfo,
wyszukiwarka gov.pl i Odyseusz (MSZ, dla krajów z tezy).

### Test na prawdziwych faktach

[`data/eval/link_facts.json`](data/eval/link_facts.json) zawiera po 2 prawdziwe
fakty z każdego linku z `links.md` (z dosłownym cytatem ze strony lub z API;
oczekiwany werdykt `confirmed`). Uruchomienie przeciw działającemu API:

```bash
python apps/backend/examples/run_link_eval.py --api http://127.0.0.1:8001 --out /tmp/link_eval.json
```

Wynik `abstain` (brak werdyktu) to bezpieczne wstrzymanie się, `wrong`
(fałsz/wprowadza w błąd) to błąd. Przypadki `volatile` (pogoda, ostrzeżenia) są
ważne tylko w dniu wygenerowania. Ostatni przebieg (21.11.2026): 64 z 81
potwierdzone, 16 bez werdyktu, 1 „fałsz” (IMGW skorygowało pomiar po
wygenerowaniu testu), mediana 12,5 s.

```bash
cd apps/backend
python -m prawdowid.ingestion sync-catalog
python -m prawdowid.ingestion ingest --source gov-rcb gov-mswia --limit 5
python -m prawdowid.ingestion ingest --collector google_fact_check
python -m prawdowid.ingestion ingest --all
python -m prawdowid.ingestion embed --limit 500          # wymaga GEMINI_API_KEY (koszt)
python -m prawdowid.ingestion stats                      # stan korpusu wobec wymagań (100 dok., 10 domen, 2 typy)
```

Te same operacje są dostępne jako zadania Celery: `ingestion.ingest_source`
i `ingestion.ingest_all` (kolejka `ingestion`, `ingest_all` synchronizuje katalog
i tworzy osobne zadanie dla każdego źródła) oraz `embedding.embed_pending`
(kolejka `embedding`, worker `worker-embedding`). Harmonogram (cron/beat) nie
jest jeszcze skonfigurowany. W Compose katalog jest montowany do workerów
tylko do odczytu (`PRAWDOWID_CATALOG`).

```bash
celery -A prawdowid.workers.celery_app call ingestion.ingest_source --args='["gov-rcb"]'
celery -A prawdowid.workers.celery_app call embedding.embed_pending --args='["passage", 200]'
```

**Dziennik zadań.** Każde wykonanie zadania Celery i każde polecenie CLI
zmieniające dane (`sync-catalog`, `ingest`, `embed`) zapisuje wiersz w tabeli
`job_run`: nazwę, wyzwalacz (`celery`/`cli`), identyfikator zadania Celery,
workera, parametry, czas trwania, podsumowanie (pobrane / nowe / niezmienione /
pominięte / błędne dokumenty, fragmenty, embeddingi), zgłoszone problemy oraz —
przy awarii — wyjątek z tracebackiem. Status: `ok`, `problems` (zakończone, ale
z błędami lub częściowo) albo `failed`. Wiersz powstaje przed rozpoczęciem pracy,
więc ślad zostaje także po awarii na starcie; niedostępność bazy nie przerywa
samego zadania. Szczegóły pobierania źródła są w `ingestion_run`, powiązanym
przez `job_id`; przebieg zgłoszeń obywateli — w `verification_run`.

```bash
python -m prawdowid.ingestion jobs                      # ostatnie zadania
python -m prawdowid.ingestion jobs --problems --since-hours 24
python -m prawdowid.ingestion jobs --name embedding.
```

**Dodanie źródła:** adapter w `adapters/sources` → kolektor w
`ingestion/collectors/` zwracający `CorpusDocument` (lub `Skipped` dla
pojedynczego nieużytecznego elementu) → rejestracja w
`ingestion/collectors/registry.py` → wpis w `catalog.toml`.

Ograniczenia: wszystkie podstrony gov.pl to jedna domena — do wymaganych
10 domen potrzebne są kolejne źródła (`stats` pokazuje brak). Konkret24 i PAP
Fakehunter nie zwracają obecnie wyników w Google Fact Check. Embeddingi
(Gemini, 768 wymiarów) są płatne i wysyłają tekst fragmentów do Google.

## Dalsze kroki

Najpierw należy zaimplementować zapis zgłoszeń, bezpieczne przekazywanie zadań
do kolejki i pełny przepływ z dowodami. Skalowanie wymaga m.in. idempotentnych
zadań, ograniczeń czasu, kontroli przeciążenia i testów obciążeniowych.
Przed wdrożeniem pobierania URL publicznie należy dodać izolację sieciową
workera i limity zgłoszeń jako kolejną warstwę ochrony. Należy też określić
retencję, ochronę danych i zasady wykorzystania źródeł.
