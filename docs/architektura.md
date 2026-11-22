# Architektura i przepływ danych

Prawdowid jest prototypem wspomagającym weryfikację informacji, a nie
automatycznym źródłem pewnej prawdy. Interfejs Next.js korzysta z API
FastAPI; PostgreSQL z pgvector przechowuje m.in. zgłoszenia, tezy, przebiegi,
dokumenty, fragmenty i relacje dowodowe. Dostępny jest także adapter wiadomości
prywatnych X.

## Przepływ

1. Użytkownik przesyła tekst, pytanie, URL, obraz lub wideo. API sprawdza
   format, rozmiar i limity oraz wykrywa próby prompt injection w polach
   tekstowych.
2. Treść jest przygotowywana do analizy: strona jest pobierana i oczyszczana,
   a dla obrazu lub wideo model odczytuje widoczny tekst i, w przypadku
   wideo, mowę. Wynik może zawierać informację o nieczytelnej części.
3. System maskuje rozpoznane dane osobowe, wyodrębnia tezy i szuka duplikatów.
   Następnie wyszukuje potencjalne dowody i wybiera fragmenty do oceny.
4. Model ocenia tezy na podstawie dostępnych materiałów. Odpowiedź zawiera
   werdykt, wyjaśnienie, źródła, ograniczenia i podpis odpowiedzi. Brak
   dowodów oraz błąd techniczny są odrębnymi stanami.
5. Zgłoszenie i przebieg są zapisywane w bazie. Panel administratora pokazuje
   statystyki i umożliwia m.in. przegląd przebiegów oraz obsługę retencji.

Przepływ i konfigurację modeli definiuje
[`dependencies.py`](../apps/backend/src/prawdowid/api/dependencies.py).
Źródła i ich aktualizację opisano w [zrodla.md](zrodla.md).

## Modele i dostawcy

| Zastosowanie | Model / dostawca | Ograniczenia |
| --- | --- | --- |
| Przygotowanie obrazów i wideo, ekstrakcja tez, ocena | Google Gemini; domyślnie `gemini-3.5-flash` dla każdego z tych etapów | Nazwy modeli można zmienić zmiennymi środowiskowymi; wyniki i dostępność zależą od usługi Google. |
| Embeddingi tez i fragmentów | Google `gemini-embedding-2`, w konfiguracji aplikacji 768 wymiarów | Wysyłanie danych do dostawcy; przy zmianie modelu lub wymiaru potrzebne jest ponowne indeksowanie. |
| Decyzje pomocnicze i jedna z warstw ochrony | TypeSafe `jev-latest` | Wymaga klucza; wybór źródeł i decyzje modelu mogą być błędne. |
| Lokalna warstwa wykrywania jailbreaków | Guardrails DetectJailbreak: `zhx123/ftrobertallm`, `sentence-transformers/all-MiniLM-L6-v2`, `GuardrailsAI/prompt-saturation-attack-detector` oraz tokenizator `google-bert/bert-base-cased` | Pliki muszą być dostępne przy uruchomieniu. Skuteczność, zwłaszcza dla języka polskiego, jest ograniczona. |

Oprócz modeli używane są zewnętrzne źródła danych, m.in. Google Fact Check
Tools API, EUvsDisinfo i X API. Google Gemini i TypeSafe otrzymują dane
potrzebne do swoich wywołań; nie należy traktować przetwarzania jako
wyłącznie lokalnego.

W skrypcie pobierania modeli Guardrails zapisano deklaracje licencji:
MIT dla `ftrobertallm`, Apache-2.0 dla `all-MiniLM-L6-v2` i tokenizatora
`bert-base-cased`; dla modelu wykrywającego prompt saturation licencja
wymaga potwierdzenia przed dystrybucją. Zob.
[`fetch_guardrails_models.py`](../apps/backend/scripts/fetch_guardrails_models.py).

Szacowanie kosztu pojedynczego sprawdzenia można skonfigurować przez
`MODEL_PRICES_USD_PER_MTOK`: dla nazw modeli podaje się ceny tokenów
wejściowych i wyjściowych w USD za milion. Jeśli brakuje ceny użytego modelu,
szacowany koszt pozostaje pusty. Nie jest to pomiar rachunku dostawcy.

## Kanał X

Kanał wymaga aplikacji X z uprawnieniami do wiadomości, konfiguracji
`X_CLIENT_ID`, `X_CLIENT_SECRET` i `X_REDIRECT_URI` oraz autoryzacji konta
przez `/auth/x`. Callback musi odpowiadać adresowi skonfigurowanemu w X.
Tokeny i stan ostatniej obsłużonej wiadomości są przechowywane na wolumenie
`x_auth_data`; tokeny dostępu są odświeżane. Po ustawieniu
`X_DM_POLL_SECONDS=60` należy uruchomić API z tą konfiguracją. Jeżeli
autoryzacja nastąpi po starcie API bez tokenu, należy ponownie uruchomić API.
Nie należy uruchamiać jednocześnie kilku instancji bota dla jednego konta.

## Ograniczenia działania

- Odczyt obrazu i wideo może pominąć tekst lub kontekst; nie jest to pełne
  wyszukiwanie odwrotne obrazu ani dowód autentyczności materiału.
- Dostępne źródła, ich aktualność i odpowiedzi modeli zmieniają się w czasie.
  Model może błędnie dopasować źródło, datę, osobę, zdarzenie lub cytat.
- Podpis odpowiedzi służy sprawdzeniu jej autentyczności, nie dowodzi
  prawdziwości werdyktu.
- Wysyłane pliki mają limit 12 MB; tekst wpisywany przez użytkownika ma limit
  2000 znaków, a dodatkowe pytanie do pliku lub linku — 500 znaków.
- Bot X jest domyślnie wyłączony. Wymaga autoryzacji konta i ustawienia
  `X_DM_POLL_SECONDS`; X API jest płatne i podlega limitom. Przy pierwszym
  uruchomieniu istniejący backlog wiadomości jest pomijany. W demonstracji
  odnotowano także wiadomość widoczną w interfejsie X, której API DM nie
  zwracało; przyczyna nie została ustalona.
- Opisane zabezpieczenia i ograniczenia wdrożenia nie zastępują przeglądu
  bezpieczeństwa ani nadzoru człowieka. Szczegóły: [ryzyka.md](ryzyka.md).
