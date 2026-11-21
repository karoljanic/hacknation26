# Prawdowid

Szkielet projektu do weryfikacji informacji: React/Next.js, Python/FastAPI,
Celery, RabbitMQ i PostgreSQL.

## Status

To fundament architektury, a nie działający fact-checker. Dostępne są adaptery
AI i przykłady ich użycia, ale aplikacja nie przyjmuje jeszcze zgłoszeń, nie
wywołuje automatycznie modeli, nie korzysta z bazy, nie indeksuje źródeł i nie
przetwarza zadań. Endpoint `/health` potwierdza tylko działanie procesu API,
nie dostępność zależności.

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
- `apps/backend/src/prawdowid/ai/adapters` — osobne kontrakty LLM, decyzji i embeddingów.
- `apps/backend/src/prawdowid/ai/instances` — implementacje Gemini i Jev.
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

## Dalsze kroki

Najpierw należy zaimplementować zapis zgłoszeń, bezpieczne przekazywanie zadań
do kolejki i pełny przepływ z dowodami. Skalowanie wymaga m.in. idempotentnych
zadań, ograniczeń czasu, kontroli przeciążenia i testów obciążeniowych.
Przed wdrożeniem pobierania URL publicznie należy dodać izolację sieciową
workera i limity zgłoszeń jako kolejną warstwę ochrony. Należy też określić
retencję, ochronę danych i zasady wykorzystania źródeł.
