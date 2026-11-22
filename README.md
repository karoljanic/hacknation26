# Prawdowid

Prawdowid to prototyp wspomagający weryfikację informacji. Przyjmuje tekst,
pytanie, link, obraz lub wideo, wyszukuje materiały źródłowe i przedstawia
ocenę wraz z uzasadnieniem oraz ograniczeniami. Wynik wymaga krytycznej
oceny, szczególnie w sprawach zdrowia i bezpieczeństwa.

## Architektura i modele

Interfejs Next.js korzysta z API FastAPI i bazy PostgreSQL z pgvector.
Zgłoszenie przechodzi przez kontrolę wejścia, przygotowanie treści,
ekstrakcję tez, wyszukiwanie dowodów i ocenę. Istnieje również adapter
wiadomości prywatnych X.

Domyślnie Google Gemini przygotowuje media, wyodrębnia i ocenia tezy oraz
tworzy embeddingi. TypeSafe Jev wspiera decyzje i wykrywanie ataków;
lokalny Guardrails stanowi dodatkową warstwę ochrony. Zewnętrzni dostawcy
otrzymują dane potrzebne do wywołań, a ich dostępność i limity wpływają na
wynik. Odczyt mediów, dobór źródeł i werdykty mogą być błędne.

Szczegóły: [architektura, przepływ, modele i ograniczenia](docs/architektura.md).

## Źródła

Katalog obejmuje źródła urzędowe, dane publiczne i organizacje
fact-checkingowe. Wpisy dokumentują kwalifikację, domenę i deklarowane
warunki wykorzystania; część licencji wymaga potwierdzenia. Korpus jest
aktualizowany przez uruchamiane zadania, a wybrane źródła są odpytywane
na żywo. Sama domena nie przesądza o prawdziwości informacji.

Szczegóły: [źródła, licencje, kwalifikacja i aktualizacja](docs/zrodla.md).

## Testy według sekcji 6

Historyczny raport obejmuje 27 scenariuszy: 16 ocenionych jako zgodne,
4 częściowo zgodne, 3 niezgodne, 1 inny uzasadniony i 3 nieuruchomione.
Mediana 21 zapytań tekstowych i pytań bez prób ataku wyniosła 17,9 s.
Nie wyliczono m.in. macro-F1, pokrycia i poprawności cytowań ani kosztu
100 testów. Test kanału X przez parametr API nie potwierdza pełnego
działania rzeczywistych wiadomości X.

Szczegóły: [wyniki, ograniczenia i dane do odtworzenia](docs/testy.md).

## Ryzyka

Najważniejsze ryzyka to halucynacje i błędne dopasowanie dowodów,
stronniczość lub luki źródeł, prywatność danych, prompt injection, awarie
i limity dostawców oraz nadużycia i koszty. Obecny panel administratora
jest demonstracyjny i przed publicznym wdrożeniem wymaga zabezpieczenia.

Szczegóły: [ryzyka i obecne ograniczenia zabezpieczeń](docs/ryzyka.md).
